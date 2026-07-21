"""Unified token estimation and context budget helpers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence
import json
import re

from models.conversation import Conversation, Message
from models.provider import Provider, provider_matches_name


DEFAULT_CONTEXT_WINDOW = 128_000
DEFAULT_RESERVED_OUTPUT_TOKENS = 4_096
MAX_RESERVED_OUTPUT_TOKENS = 20_000
WARNING_THRESHOLD = 0.80
COMPACT_THRESHOLD = 0.90
DANGER_THRESHOLD = 0.95


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
            count += _estimate_text_tokens(json.dumps(tool_call, ensure_ascii=False, sort_keys=True, default=str))
            result = tool_call.get("result")
            if result is not None:
                count += _estimate_text_tokens(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))

    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        count += _estimate_text_tokens(json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str))

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


@dataclass(frozen=True)
class TokenBudget:
    context_window: int
    reserved_output_tokens: int
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

    @property
    def context_window(self) -> int:
        return int(self.budget.context_window or 0)

    @property
    def reserved_output_tokens(self) -> int:
        return int(self.budget.reserved_output_tokens or 0)

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
            f"预留输出 {format_token_count(self.reserved_output_tokens)}",
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
    mode_context_window_limit: int | None = None,
) -> TokenBudget:
    conversation = conversation if isinstance(conversation, Conversation) else None
    provider_id = provider_id or str(getattr(conversation, "provider_id", "") or "").strip()
    provider_name = provider_name or str(getattr(conversation, "provider_name", "") or "").strip()
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
    mode_window = _coerce_positive_int(mode_context_window_limit) or 0
    context_window = profile_window or mode_window or DEFAULT_CONTEXT_WINDOW

    reserved_output_tokens = (
        _coerce_positive_int(getattr(profile, "max_output_tokens", None))
        or DEFAULT_RESERVED_OUTPUT_TOKENS
    )
    reserved_output_tokens = min(reserved_output_tokens, MAX_RESERVED_OUTPUT_TOKENS)
    if context_window <= reserved_output_tokens:
        reserved_output_tokens = max(0, min(reserved_output_tokens, max(context_window - 1, 0)))

    effective_prompt_limit = max(context_window - reserved_output_tokens, 0)
    warning_threshold_tokens = int(effective_prompt_limit * WARNING_THRESHOLD)
    compact_threshold_tokens = int(effective_prompt_limit * COMPACT_THRESHOLD)
    danger_threshold_tokens = int(effective_prompt_limit * DANGER_THRESHOLD)

    provider_name_value = str(getattr(resolved_provider, "name", "") or provider_name or "").strip()
    model_id_value = str(model_id or getattr(profile, "model_id", "") or "").strip()
    profile_display_name = str(getattr(profile, "display_name", "") or "").strip()

    return TokenBudget(
        context_window=context_window,
        reserved_output_tokens=reserved_output_tokens,
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
    mode_context_window_limit: int | None = None,
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
        mode_context_window_limit=mode_context_window_limit,
    )
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
    elif usage_ratio >= COMPACT_THRESHOLD:
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
    )
