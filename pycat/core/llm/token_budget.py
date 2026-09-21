"""Unified token estimation and context budget helpers."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from pycat.models.conversation import Conversation, Message, normalize_tool_result
from pycat.models.llm_config import LLMConfig
from pycat.models.model_ref import provider_matches_name
from pycat.models.provider import Provider

DEFAULT_CONTEXT_WINDOW = 128_000
DEFAULT_OUTPUT_LIMIT = 65_536
WARNING_THRESHOLD = 0.80
DANGER_THRESHOLD = 0.95
IMAGE_TOKEN_ESTIMATE = 256
REQUEST_USAGE_METADATA_KEY = "request_usage"


def _coerce_positive_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except Exception:
        return None
    return number if number > 0 else None


def _estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    other_chars = len(text) - chinese_chars
    whitespace_penalty = max(0, text.count("\n") - 1)
    return int(chinese_chars * 1.0 + other_chars * 0.25 + whitespace_penalty * 0.5)


def estimate_tokens(value: Any) -> int:
    if isinstance(value, Conversation):
        return estimate_conversation_tokens(value)
    if isinstance(value, Message):
        return estimate_message_tokens(value)
    if value is None:
        return 0
    if isinstance(value, str):
        return _estimate_text_tokens(value)
    if isinstance(value, dict):
        return _estimate_text_tokens(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
    if isinstance(value, (list, tuple, set)):
        return sum(estimate_tokens(item) for item in value)
    return _estimate_text_tokens(str(value))


def estimate_request_tokens(payload: Any) -> int:
    """Estimate one provider payload without charging image transport bytes.

    OpenAI-compatible, Responses, Anthropic and Ollama serialize the same
    logical image through different JSON shapes.  Base64 is transport data,
    not text visible to the model, so every recognized image part contributes
    one bounded visual estimate while the remaining payload is estimated as
    ordinary JSON.
    """

    budget_view, image_count = _request_budget_view(payload)
    text_tokens = _estimate_text_tokens(
        json.dumps(budget_view, ensure_ascii=False, sort_keys=True, default=str)
    )
    return max(0, text_tokens + image_count * IMAGE_TOKEN_ESTIMATE)


def _request_budget_view(value: Any) -> tuple[Any, int]:
    if isinstance(value, dict):
        item_type = str(value.get("type") or "").strip().lower()
        if item_type == "image_url" and isinstance(value.get("image_url"), dict):
            image = value.get("image_url") or {}
            return {"type": "image_url", "detail": image.get("detail")}, 1
        if item_type == "input_image" and value.get("image_url"):
            return {"type": "input_image"}, 1
        if item_type == "image" and isinstance(value.get("source"), dict):
            source = value.get("source") or {}
            if str(source.get("type") or "").strip().lower() == "base64" and source.get("data"):
                return {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": str(source.get("media_type") or ""),
                    },
                }, 1

        sanitized: dict[Any, Any] = {}
        image_count = 0
        for key, item in value.items():
            if key == "images" and isinstance(item, list):
                visual_items = [image for image in item if str(image or "").strip()]
                sanitized[key] = ["<image>" for _ in visual_items]
                image_count += len(visual_items)
                continue
            child, child_images = _request_budget_view(item)
            sanitized[key] = child
            image_count += child_images
        return sanitized, image_count

    if isinstance(value, (list, tuple)):
        sanitized_items: list[Any] = []
        image_count = 0
        for item in value:
            child, child_images = _request_budget_view(item)
            sanitized_items.append(child)
            image_count += child_images
        return sanitized_items, image_count

    if isinstance(value, set):
        return _request_budget_view(sorted(value, key=str))
    if isinstance(value, str) and value.lower().startswith("data:image/") and ";base64," in value[:128].lower():
        return "<image>", 1
    return value, 0


def estimate_message_tokens(message: Message | dict[str, Any] | Any) -> int:
    if isinstance(message, Message):
        payload = message.to_dict()
    elif isinstance(message, dict):
        payload = dict(message)
    else:
        payload = {"content": getattr(message, "content", "") or ""}

    count = 4
    count += _estimate_text_tokens(str(payload.get("content") or ""))
    count += _estimate_text_tokens(str(payload.get("thinking") or ""))

    images = payload.get("images") or []
    if isinstance(images, list):
        count += len([item for item in images if str(item or "").strip()]) * 256

    tool_calls = payload.get("tool_calls") or []
    if isinstance(tool_calls, list):
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            count += 8
            visible_call = {
                key: tool_call[key]
                for key in ("id", "type", "function")
                if key in tool_call
            }
            count += _estimate_text_tokens(
                json.dumps(visible_call, ensure_ascii=False, sort_keys=True, default=str)
            )
            result = tool_call.get("result")
            if result is not None:
                payload = normalize_tool_result(result)
                content = payload.get("content")
                if isinstance(content, str):
                    count += _estimate_text_tokens(content)
                elif content is not None:
                    count += _estimate_text_tokens(
                        json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)
                    )
                result_images = {
                    str(item)
                    for item in (*list(tool_call.get("result_images") or []), *list(payload.get("images") or []))
                    if str(item or "").strip()
                }
                count += len(result_images) * IMAGE_TOKEN_ESTIMATE

    if str(payload.get("role") or "") == "tool":
        count += 4

    return max(0, count)


def estimate_conversation_tokens(conversation: Conversation | Sequence[Message] | Iterable[Message]) -> int:
    if isinstance(conversation, Conversation):
        messages = list(getattr(conversation, "messages", []) or [])
    else:
        messages = list(conversation or [])
    total = 0
    for message in messages:
        if getattr(message, "archived_content_id", None):
            continue
        total += estimate_message_tokens(message)
    return total


def _resolve_provider(
    *,
    provider: Provider | None = None,
    providers: Sequence[Provider] | None = None,
    provider_id: str = "",
    provider_name: str = "",
) -> Provider | None:
    if provider is not None:
        return provider
    provider_id = str(provider_id or "").strip()
    provider_name = str(provider_name or "").strip()
    if not providers:
        return None
    for item in providers:
        if provider_id and str(getattr(item, "id", "") or "") == provider_id:
            return item
    if provider_name:
        for item in providers:
            if provider_matches_name(item, provider_name):
                return item
    return None


def _resolve_model_profile(
    *,
    provider: Provider | None = None,
    providers: Sequence[Provider] | None = None,
    provider_id: str = "",
    provider_name: str = "",
    model_id: str = "",
) -> Any:
    resolved_provider = _resolve_provider(
        provider=provider,
        providers=providers,
        provider_id=provider_id,
        provider_name=provider_name,
    )
    if resolved_provider is None:
        return None
    resolved_model = str(model_id or "").strip()
    if not resolved_model:
        return None
    try:
        return resolved_provider.find_model_profile(resolved_model)
    except Exception:
        return None


def _profile_output_override(provider: Provider | None, profile: Any) -> int | None:
    extra_body = getattr(profile, "extra_body", None)
    if not isinstance(extra_body, dict) or provider is None:
        return None
    if provider.is_openai_responses:
        return _coerce_positive_int(extra_body.get("max_output_tokens"))
    if provider.is_ollama_chat:
        options = extra_body.get("options")
        if isinstance(options, dict):
            return _coerce_positive_int(options.get("num_predict"))
        return None
    return _coerce_positive_int(extra_body.get("max_tokens"))


@dataclass(frozen=True)
class TokenBudget:
    context_window: int
    output_limit: int
    effective_prompt_limit: int
    warning_threshold_tokens: int
    compact_threshold_tokens: int
    danger_threshold_tokens: int
    provider_name: str = ""
    model_id: str = ""
    profile_display_name: str = ""


@dataclass(frozen=True)
class TokenUsageSnapshot:
    context_tokens: int
    total_messages: int
    active_messages: int
    budget: TokenBudget
    assistant_tokens: int = 0
    remaining_window_tokens: int = 0
    remaining_prompt_tokens: int = 0
    usage_ratio: float = 0.0
    status: str = "ok"
    source: str = "conversation_estimate"
    replay_pressure: str = ""

    @property
    def context_window(self) -> int:
        return int(self.budget.context_window or 0)

    @property
    def output_limit(self) -> int:
        return int(self.budget.output_limit or 0)

    @property
    def effective_prompt_limit(self) -> int:
        return int(self.budget.effective_prompt_limit or 0)

    @property
    def warning_threshold_tokens(self) -> int:
        return int(self.budget.warning_threshold_tokens or 0)

    @property
    def compact_threshold_tokens(self) -> int:
        return int(self.budget.compact_threshold_tokens or 0)

    @property
    def danger_threshold_tokens(self) -> int:
        return int(self.budget.danger_threshold_tokens or 0)

    def short_label(self) -> str:
        used = format_token_count(self.context_tokens)
        limit = format_token_count(self.effective_prompt_limit or self.context_window)
        return f"上下文 {used}/{limit}"

    def detail_text(self) -> str:
        parts = [
            f"窗口 {format_token_count(self.context_window)}",
            f"输出上限 {format_token_count(self.output_limit)}",
            f"剩余 {format_token_count(self.remaining_prompt_tokens)}",
        ]
        if self.budget.model_id:
            parts.append(f"模型 {self.budget.model_id}")
        if self.budget.provider_name:
            parts.append(f"提供方 {self.budget.provider_name}")
        if self.status != "ok":
            parts.append(f"状态 {self.status}")
        return " · ".join(parts)


def format_token_count(tokens: int | None) -> str:
    value = int(tokens or 0)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 10_000:
        return f"{value / 1_000:.1f}k"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return f"{value:,}"


def resolve_token_budget(
    conversation: Conversation | None = None,
    *,
    provider: Provider | None = None,
    providers: Sequence[Provider] | None = None,
    provider_id: str = "",
    provider_name: str = "",
    model_id: str = "",
    llm_config: LLMConfig | None = None,
    request_output_limit: int | None = None,
    compact_threshold_ratio: float = 0.80,
) -> TokenBudget:
    conversation = conversation if isinstance(conversation, Conversation) else None
    provider_id = provider_id or str(getattr(conversation, "provider_id", "") or "").strip()
    provider_name = provider_name or str(getattr(conversation, "provider_name", "") or "").strip()
    request_config = llm_config or (
        LLMConfig.from_conversation(conversation) if conversation is not None else None
    )
    if request_config is not None:
        provider_id = provider_id or request_config.provider_id
        provider_name = provider_name or request_config.provider_name
        model_id = model_id or request_config.resolved_model()
    model_id = model_id or str(getattr(conversation, "model", "") or "").strip()

    resolved_provider = _resolve_provider(
        provider=provider,
        providers=providers,
        provider_id=provider_id,
        provider_name=provider_name,
    )
    profile = _resolve_model_profile(
        provider=resolved_provider,
        providers=providers,
        provider_id=provider_id,
        provider_name=provider_name,
        model_id=model_id,
    )

    profile_window = _coerce_positive_int(getattr(profile, "context_window", None)) or 0
    context_window = profile_window or DEFAULT_CONTEXT_WINDOW

    extra_output_limit = _profile_output_override(resolved_provider, profile)
    configured_output_limit = _coerce_positive_int(request_output_limit)
    if configured_output_limit is None and request_config is not None:
        configured_output_limit = _coerce_positive_int(request_config.max_tokens)
    profile_output_limit = _coerce_positive_int(getattr(profile, "max_output_tokens", None))
    # A model profile describes a capability ceiling, not the request default.
    # Keeping the product default independent prevents a stale catalog entry
    # (for example, an old 4,096 value) from silently truncating reasoning.
    output_limit = extra_output_limit or configured_output_limit or DEFAULT_OUTPUT_LIMIT
    if profile_output_limit is not None:
        output_limit = min(output_limit, profile_output_limit)
    if context_window <= output_limit:
        output_limit = max(0, min(output_limit, max(context_window - 1, 0)))

    effective_prompt_limit = max(context_window - output_limit, 0)
    warning_threshold_tokens = int(effective_prompt_limit * WARNING_THRESHOLD)
    compact_ratio = max(0.10, min(0.95, float(compact_threshold_ratio or 0.80)))
    compact_threshold_tokens = int(effective_prompt_limit * compact_ratio)
    danger_threshold_tokens = int(effective_prompt_limit * DANGER_THRESHOLD)

    provider_name_value = str(getattr(resolved_provider, "name", "") or provider_name or "").strip()
    model_id_value = str(model_id or getattr(profile, "model_id", "") or "").strip()
    profile_display_name = str(getattr(profile, "display_name", "") or "").strip()

    return TokenBudget(
        context_window=context_window,
        output_limit=output_limit,
        effective_prompt_limit=effective_prompt_limit,
        warning_threshold_tokens=warning_threshold_tokens,
        compact_threshold_tokens=compact_threshold_tokens,
        danger_threshold_tokens=danger_threshold_tokens,
        provider_name=provider_name_value,
        model_id=model_id_value,
        profile_display_name=profile_display_name,
    )


def build_token_usage_snapshot(
    conversation: Conversation | None,
    *,
    providers: Sequence[Provider] | None = None,
    provider: Provider | None = None,
    provider_id: str = "",
    provider_name: str = "",
    model_id: str = "",
    compact_threshold_ratio: float = 0.80,
    request_usage: dict[str, Any] | None = None,
) -> TokenUsageSnapshot | None:
    if conversation is None:
        return None

    budget = resolve_token_budget(
        conversation,
        provider=provider,
        providers=providers,
        provider_id=provider_id,
        provider_name=provider_name,
        model_id=model_id,
        compact_threshold_ratio=compact_threshold_ratio,
    )
    request_snapshot = _request_usage_snapshot(
        conversation,
        request_usage=request_usage,
        current_budget=budget,
        provider_id=provider_id,
        model_id=model_id,
    )
    if request_snapshot is not None:
        return request_snapshot

    messages = [msg for msg in getattr(conversation, "messages", []) or [] if not getattr(msg, "archived_content_id", None)]
    context_tokens = estimate_conversation_tokens(messages)
    assistant_tokens = 0
    for msg in messages:
        if getattr(msg, "role", "") == "assistant" and getattr(msg, "tokens", None):
            try:
                assistant_tokens += int(msg.tokens or 0)
            except Exception:
                continue

    active_messages = len(messages)
    remaining_window_tokens = max(budget.context_window - context_tokens, 0)
    remaining_prompt_tokens = max(budget.effective_prompt_limit - context_tokens, 0)
    if budget.effective_prompt_limit > 0:
        usage_ratio = context_tokens / budget.effective_prompt_limit
    else:
        usage_ratio = 0.0
    if usage_ratio >= 1.0:
        status = "danger"
    elif usage_ratio >= DANGER_THRESHOLD:
        status = "danger"
    elif usage_ratio >= max(0.10, min(0.95, float(compact_threshold_ratio or 0.80))):
        status = "compact"
    elif usage_ratio >= WARNING_THRESHOLD:
        status = "warning"
    else:
        status = "ok"

    return TokenUsageSnapshot(
        context_tokens=context_tokens,
        total_messages=len(getattr(conversation, "messages", []) or []),
        active_messages=active_messages,
        budget=budget,
        assistant_tokens=assistant_tokens,
        remaining_window_tokens=remaining_window_tokens,
        remaining_prompt_tokens=remaining_prompt_tokens,
        usage_ratio=usage_ratio,
        status=status,
        source="conversation_estimate",
    )


def request_usage_payload(
    *,
    token_estimate: int,
    budget: TokenBudget,
    replay_pressure: str,
    active_messages: int,
    provider_id: str = "",
) -> dict[str, Any]:
    """Serialize the small budget projection for one immutable provider body."""
    return {
        "source": "provider_request",
        "token_estimate": max(0, int(token_estimate or 0)),
        "active_messages": max(0, int(active_messages or 0)),
        "context_window": int(budget.context_window or 0),
        "output_limit": int(budget.output_limit or 0),
        "effective_prompt_limit": int(budget.effective_prompt_limit or 0),
        "warning_threshold_tokens": int(budget.warning_threshold_tokens or 0),
        "compact_threshold_tokens": int(budget.compact_threshold_tokens or 0),
        "danger_threshold_tokens": int(budget.danger_threshold_tokens or 0),
        "provider_id": str(provider_id or ""),
        "provider_name": str(budget.provider_name or ""),
        "model_id": str(budget.model_id or ""),
        "profile_display_name": str(budget.profile_display_name or ""),
        "replay_pressure": str(replay_pressure or "normal"),
    }


def _request_usage_snapshot(
    conversation: Conversation,
    *,
    request_usage: dict[str, Any] | None,
    current_budget: TokenBudget,
    provider_id: str,
    model_id: str,
) -> TokenUsageSnapshot | None:
    usage = dict(request_usage) if isinstance(request_usage, dict) else _latest_request_usage(conversation)
    if not usage:
        return None
    try:
        effective_prompt_limit = int(usage.get("effective_prompt_limit") or 0)
        context_window = int(usage.get("context_window") or 0)
        context_tokens = max(0, int(usage.get("token_estimate") or 0))
    except Exception:
        return None
    if effective_prompt_limit <= 0 or context_window <= 0:
        return None

    current_model = str(model_id or current_budget.model_id or getattr(conversation, "model", "") or "").strip()
    usage_model = str(usage.get("model_id") or "").strip()
    if current_model and usage_model and current_model != usage_model:
        return None
    current_provider_id = str(provider_id or getattr(conversation, "provider_id", "") or "").strip()
    usage_provider_id = str(usage.get("provider_id") or "").strip()
    if current_provider_id and usage_provider_id and current_provider_id != usage_provider_id:
        return None

    budget = TokenBudget(
        context_window=context_window,
        output_limit=max(0, int(usage.get("output_limit") or 0)),
        effective_prompt_limit=effective_prompt_limit,
        warning_threshold_tokens=max(0, int(usage.get("warning_threshold_tokens") or 0)),
        compact_threshold_tokens=max(0, int(usage.get("compact_threshold_tokens") or 0)),
        danger_threshold_tokens=max(0, int(usage.get("danger_threshold_tokens") or 0)),
        provider_name=str(usage.get("provider_name") or current_budget.provider_name or ""),
        model_id=usage_model or current_model,
        profile_display_name=str(usage.get("profile_display_name") or ""),
    )
    ratio = context_tokens / effective_prompt_limit
    if ratio >= 1.0 or context_tokens >= max(1, budget.danger_threshold_tokens):
        status = "danger"
    elif context_tokens >= max(1, budget.compact_threshold_tokens):
        status = "compact"
    elif context_tokens >= max(1, budget.warning_threshold_tokens):
        status = "warning"
    else:
        status = "ok"
    messages = list(getattr(conversation, "messages", []) or [])
    return TokenUsageSnapshot(
        context_tokens=context_tokens,
        total_messages=len(messages),
        active_messages=max(0, int(usage.get("active_messages") or 0)),
        budget=budget,
        remaining_window_tokens=max(context_window - context_tokens, 0),
        remaining_prompt_tokens=max(effective_prompt_limit - context_tokens, 0),
        usage_ratio=ratio,
        status=status,
        source="provider_request",
        replay_pressure=str(usage.get("replay_pressure") or "normal"),
    )


def _latest_request_usage(conversation: Conversation) -> dict[str, Any] | None:
    """Return only a request snapshot that still follows the latest real User."""
    for message in reversed(list(getattr(conversation, "messages", []) or [])):
        role = str(getattr(message, "role", "") or "")
        if role == "user":
            return None
        if role != "assistant":
            continue
        metadata = getattr(message, "metadata", {}) or {}
        usage = metadata.get(REQUEST_USAGE_METADATA_KEY) if isinstance(metadata, dict) else None
        if isinstance(usage, dict):
            return dict(usage)
    return None
