from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional

from pycat.core.content.attachments import encode_image_file_to_data_url
from pycat.core.llm.ollama_codec import messages_from_openai as _openai_messages_to_ollama
from pycat.core.llm.reasoning import (
    CHAT_REASONING_CODEC,
    apply_reasoning,
    normalize_reasoning_codec,
)
from pycat.core.llm.token_budget import TokenBudget, resolve_token_budget
from pycat.models.conversation import Conversation, Message, normalize_tool_result
from pycat.models.llm_config import LLMConfig
from pycat.models.provider import Provider

logger = logging.getLogger(__name__)
_ANTHROPIC_SYSTEM_ROLE = "system"
_RUNTIME_ERROR_PREFIXES = (
    "http 错误",
    "模型调用失败：",
    "错误:",
    "error sending message:",
)
_ASSISTANT_ROLE_PREFIX_RE = re.compile(r"^\s*(?:assistant\s*:\s*)+", re.IGNORECASE)
_USER_ROLE_PREFIX_RE = re.compile(r"^\s*user\s*:\s*", re.IGNORECASE)
_REASONING_ITEMS_KEY = "_pycat_reasoning_items"
_TOOL_TRANSCRIPT_MARKER_RE = re.compile(
    r"(?:^|\s)tool\s+"
    r"[a-z0-9_]+(?:__[a-z0-9_]+|_[a-z0-9_]+)?"
    r"(?:\s*,\s*[a-z0-9_]+(?:__[a-z0-9_]+|_[a-z0-9_]+)?)*\s*:",
    re.IGNORECASE,
)


def _is_openrouter_provider(provider: Provider) -> bool:
    # OpenRouter is only a route marker for the Chat Completions envelope.
    # Keep the check on Provider so discovery and request construction share
    # exactly the same boundary.
    return bool(getattr(provider, "is_openrouter_route", False))


def _normalize_image_url(image: str) -> str:
    if image.startswith("data:") or image.startswith(("http://", "https://")):
        return image
    return encode_image_file_to_data_url(image) or ""


def _build_multimodal_content(
    text_content: Any,
    images: list[str],
    provider: Provider,
    *,
    supports_vision: bool | None = None,
) -> Any:
    vision_enabled = False if supports_vision is None else bool(supports_vision)
    if not images or not vision_enabled:
        if images and not vision_enabled:
            logger.warning("Images omitted because the selected model does not support vision")
        return text_content

    content_list: list[dict[str, Any]] = []
    if text_content:
        content_list.append({"type": "text", "text": text_content})

    for image in images:
        if not isinstance(image, str) or not image:
            continue
        image_url = _normalize_image_url(image)
        if image_url:
            content_list.append({"type": "image_url", "image_url": {"url": image_url}})

    return content_list or text_content


def _build_message_content(msg: Message, provider: Provider, *, supports_vision: bool | None = None) -> Any:
    text_content = msg.summary if msg.summary else msg.content
    if isinstance(text_content, str):
        if msg.role == "assistant":
            text_content = _clean_assistant_content_for_api(text_content, trim_tool_transcript=False)
        elif msg.role == "user" and msg.summary:
            text_content = _USER_ROLE_PREFIX_RE.sub("", text_content).strip()
        if msg.role == 'user' and msg.metadata.get('mentions'):
            text_content += '\n\nSelected references (identity only; actions still require an explicit request):\n' + json.dumps(
                msg.metadata['mentions'], ensure_ascii=False)
    return _build_multimodal_content(
        text_content,
        list(getattr(msg, "images", []) or []),
        provider,
        supports_vision=supports_vision,
    )


def _build_assistant_tool_call_content(
    msg: Message,
    provider: Provider,
    *,
    supports_vision: bool | None = None,
) -> Any:
    """Return assistant text for replaying a tool-call turn.

    Per-message summaries are archive notes, not provider-native assistant
    content. Replaying those summaries alongside the original tool_calls creates
    nested transcripts such as ``assistant: assistant: ... tool file__read:``.
    """
    content = msg.content
    if isinstance(content, str):
        content = _clean_assistant_content_for_api(content, trim_tool_transcript=True)
    return _build_multimodal_content(
        content,
        list(getattr(msg, "images", []) or []),
        provider,
        supports_vision=supports_vision,
    )


def _clean_assistant_content_for_api(text: str, *, trim_tool_transcript: bool) -> str:
    clean = str(text or "").strip()
    while True:
        stripped = _ASSISTANT_ROLE_PREFIX_RE.sub("", clean).strip()
        if stripped == clean:
            break
        clean = stripped
    if trim_tool_transcript:
        marker = _TOOL_TRANSCRIPT_MARKER_RE.search(clean)
        if marker:
            clean = clean[: marker.start()].strip()
    return clean


def _tool_result_content_for_api(
    result: Any,
    *,
    tool_result_renderer: Callable[[Any], str] | None = None,
) -> str:
    if tool_result_renderer is not None:
        return tool_result_renderer(result)
    return _default_tool_result_content(result)


def _default_tool_result_content(result: Any) -> str:
    payload = normalize_tool_result(result)
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    content = payload.get("content")
    summary = str(metadata.get("tool_result_summary") or payload.get("summary") or "").strip()
    replay_view = str(metadata.get("tool_result_replay_view") or "").strip()
    if replay_view in {"summary", "ccr"}:
        archived = _render_archived_tool_result(payload, metadata, summary=summary)
        if archived:
            return archived
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    try:
        return json.dumps(content, ensure_ascii=False)
    except Exception:
        return str(content)


def _render_archived_tool_result(payload: dict[str, Any], metadata: dict[str, Any], *, summary: str) -> str:
    content_id = str(metadata.get("content_id") or metadata.get("archive_content_id") or "").strip()
    total_chars = metadata.get("tool_result_chars") or metadata.get("archive_size")
    source = str(metadata.get("name") or "tool").strip() or "tool"
    if not summary:
        summary = str(metadata.get("tool_result_summary") or payload.get("summary") or "").strip()
    if not (summary or content_id):
        return ""
    lines = ["[tool_result:archived]", f"source={source}"]
    if total_chars:
        lines.append(f"chars={total_chars}")
    if content_id:
        lines.append(f"content_id={content_id}")
    if summary:
        lines.append(f"summary={summary[:500]}")
    if content_id:
        lines.append(
            f'Use archive__read(content_id="{content_id}", view="content", offset=0) '
            "to restore exact content."
        )
    return "\n".join(lines)


def _effective_model_profile(
    provider: Provider,
    conversation: Conversation | None = None,
    llm_config: LLMConfig | None = None,
):
    request_cfg = llm_config or (
        LLMConfig.from_conversation(conversation) if conversation is not None else LLMConfig()
    )
    model_id = request_cfg.resolved_model()
    if not model_id:
        model_ids = provider.model_ids()
        model_id = model_ids[0] if model_ids else ""
    return provider.effective_model_profile(model_id)


def _assistant_has_reasoning(msg: Message) -> bool:
    if bool(str(getattr(msg, "thinking", "") or "").strip()):
        return True
    metadata = getattr(msg, "metadata", {}) or {}
    if not isinstance(metadata, dict):
        return False
    state = metadata.get("reasoning_state")
    return bool(metadata.get("thinking_present")) or (
        isinstance(state, dict) and isinstance(state.get("items"), list) and bool(state["items"])
    )


def _attach_reasoning_replay(
    payload: dict[str, Any],
    msg: Message,
    provider: Provider,
    *,
    profile: Any,
) -> None:
    metadata = getattr(msg, "metadata", {}) or {}
    state = metadata.get("reasoning_state") if isinstance(metadata, dict) else None
    if provider.is_openai_responses and metadata.get('responses_phase') in {'commentary', 'final_answer'}:
        payload['phase'] = metadata['responses_phase']
    codec = normalize_reasoning_codec(getattr(profile, "reasoning_codec", "none"))
    if isinstance(state, dict) and str(state.get("codec") or "") == codec:
        items = state.get("items")
        if isinstance(items, list) and items:
            # OpenRouter's compatible endpoint requires its opaque
            # reasoning_details array.  Other Chat Completions endpoints must
            # not receive that private field, even though they share the same
            # canonical codec.
            if codec == CHAT_REASONING_CODEC and _is_openrouter_provider(provider):
                payload["reasoning_details"] = copy.deepcopy(items)
            else:
                payload[_REASONING_ITEMS_KEY] = copy.deepcopy(items)
            return
    if codec == CHAT_REASONING_CODEC and _is_openrouter_provider(provider):
        # A visible thinking string is not a valid OpenRouter continuation;
        # only the opaque reasoning_details state is safe to replay.
        return
    if not _assistant_has_reasoning(msg):
        return
    if provider.is_ollama_chat:
        payload["thinking"] = msg.thinking or ""
    elif provider.is_chat_completions_like:
        key = str(metadata.get("thinking_key") or "reasoning_content")
        payload[key] = msg.thinking or ""


def _is_runtime_error_message(msg: Message) -> bool:
    metadata = getattr(msg, "metadata", {}) or {}
    if isinstance(metadata, dict) and metadata.get("runtime_error"):
        return True

    text = str(getattr(msg, "content", "") or "").strip().lower()
    if not text:
        return False
    if any(text.startswith(prefix) for prefix in _RUNTIME_ERROR_PREFIXES):
        return True
    return "invalid_request_error" in text and "reasoning_content" in text


def _should_replay_reasoning(
    messages: List[Message],
    provider: Provider,
) -> bool:
    if not provider.is_chat_completions_like:
        return False
    return any(m.role == "assistant" and _assistant_has_reasoning(m) for m in messages)


def _tool_call_names(tool_calls: Any) -> list[str]:
    names: list[str] = []
    for tool_call in tool_calls or []:
        if not isinstance(tool_call, dict):
            continue
        func = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
        name = str(func.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def _tool_call_summary_lines(
    tool_calls: Any,
    *,
    tool_result_renderer: Callable[[Any], str] | None = None,
) -> list[str]:
    lines: list[str] = []
    for tool_call in tool_calls or []:
        if not isinstance(tool_call, dict):
            continue
        func = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
        name = str(func.get("name") or "unknown_tool").strip() or "unknown_tool"
        summary = str(tool_call.get("result_summary") or "").strip()
        result = _tool_result_content_for_api(
            tool_call.get("result"),
            tool_result_renderer=tool_result_renderer,
        ).strip()
        if not summary and result:
            summary = result.splitlines()[0].strip()[:220]
        if summary:
            lines.append(f"- {name}: {summary}")
        else:
            lines.append(f"- {name}")
    return lines


def _recover_assistant_as_user(
    msg: Message,
    *,
    tool_result_renderer: Callable[[Any], str] | None = None,
) -> Message | None:
    sections: list[str] = []

    content = str(getattr(msg, "content", "") or "").strip()
    if content:
        sections.append(
            "[Recovered assistant context: previous assistant reply omitted from reasoning replay]\n"
            + content
        )

    tool_names = _tool_call_names(getattr(msg, "tool_calls", None))
    if tool_names:
        bullet_lines = "\n".join(f"- {name}" for name in tool_names[:8])
        more = "\n- ..." if len(tool_names) > 8 else ""
        sections.append(
            "[Recovered assistant tool request: previous tool-call step omitted from reasoning replay]\n"
            f"Requested tools:\n{bullet_lines}{more}"
        )

    tool_summaries = _tool_call_summary_lines(
        getattr(msg, "tool_calls", None),
        tool_result_renderer=tool_result_renderer,
    )
    if tool_summaries:
        joined = "\n".join(tool_summaries[:8])
        more = "\n- ..." if len(tool_summaries) > 8 else ""
        sections.append(
            "[Recovered tool outputs for the omitted tool-call step]\n"
            f"{joined}{more}"
        )

    if not sections:
        return None

    return Message(
        role="user",
        content="\n\n".join(sections),
        metadata={"synthetic": True, "context_kind": "recovered_assistant"},
    )


def _sanitize_reasoning_history(
    messages: List[Message],
    provider: Provider,
    *,
    conversation: Conversation | None = None,
    tool_result_renderer: Callable[[Any], str] | None = None,
) -> List[Message]:
    filtered: List[Message] = []
    for msg in messages:
        metadata = getattr(msg, "metadata", {}) or {}
        if msg.role == "assistant" and bool(metadata.get("incomplete")):
            content = str(getattr(msg, "content", "") or "").strip()
            if content:
                clone = Message.from_dict(msg.to_dict())
                clone.content = content
                clone.thinking = None
                clone.tool_calls = None
                for key in ("reasoning_state", "thinking_present", "thinking_hidden", "thinking_key"):
                    clone.metadata.pop(key, None)
                filtered.append(clone)
            continue
        filtered.append(msg)

    if not _should_replay_reasoning(filtered, provider):
        return filtered

    sanitized: List[Message] = []
    for msg in filtered:

        if msg.role == "assistant" and not _assistant_has_reasoning(msg):
            if _is_runtime_error_message(msg):
                continue
            recovered = _recover_assistant_as_user(msg, tool_result_renderer=tool_result_renderer)
            if recovered is not None:
                sanitized.append(recovered)
            continue

        if msg.role == "tool":
            continue

        sanitized.append(msg)

    return sanitized

def build_api_messages(
    messages: List[Message],
    provider: Provider,
    *,
    conversation: Conversation | None = None,
    tool_result_renderer: Callable[[Any], str] | None = None,
) -> List[Dict[str, Any]]:
    profile = _effective_model_profile(provider, conversation)
    supports_vision = profile.supports_input("image")
    messages = _sanitize_reasoning_history(
        messages,
        provider,
        conversation=conversation,
        tool_result_renderer=tool_result_renderer,
    )
    api_messages: List[Dict[str, Any]] = []

    # Safety check: warn if no user messages in input
    has_user = any(m.role == "user" for m in messages)
    if not has_user:
        logger.warning("build_api_messages: no user messages found — context may be corrupted")

    for msg in messages:
        if msg.role == "tool":
            continue
        if msg.role == "assistant" and _is_runtime_error_message(msg):
            continue

        message_payload = {
            "role": msg.role,
            "content": _build_message_content(
                msg,
                provider,
                supports_vision=supports_vision,
            ),
        }

        if msg.tool_calls and msg.role == "assistant":
            tool_calls_with_results: List[Dict[str, Any]] = []
            for tc in msg.tool_calls:
                if not isinstance(tc, dict):
                    continue
                tc_id = tc.get("id")
                if not tc_id:
                    continue
                result = tc.get("result")
                result_images = list(tc.get("result_images") or [])
                if result is not None:
                    result = _tool_result_content_for_api(result, tool_result_renderer=tool_result_renderer)
                if result_images:
                    result = _build_multimodal_content(
                        result,
                        result_images,
                        provider,
                        supports_vision=supports_vision,
                    )
                if result is None:
                    continue

                clean_tc = {k: v for k, v in tc.items() if k in ("id", "type", "function")}
                tool_calls_with_results.append(
                    {
                        "clean": clean_tc,
                        "result": result,
                        "id": tc_id,
                    }
                )

            if tool_calls_with_results:
                assistant_payload: Dict[str, Any] = {
                    "role": "assistant",
                    "content": _build_assistant_tool_call_content(
                        msg,
                        provider,
                        supports_vision=supports_vision,
                    ),
                    "tool_calls": [tc["clean"] for tc in tool_calls_with_results],
                }

                _attach_reasoning_replay(assistant_payload, msg, provider, profile=profile)

                api_messages.append(assistant_payload)
                for tc in tool_calls_with_results:
                    api_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": tc["result"],
                        }
                    )
                continue

        if msg.role == "assistant":
            _attach_reasoning_replay(message_payload, msg, provider, profile=profile)

        api_messages.append(message_payload)

    return api_messages


def _anthropic_content_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list):
        blocks: list[dict[str, Any]] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                blocks.append({"type": "text", "text": str(item.get("text") or "")})
            elif isinstance(item, dict) and item.get("type") == "image_url":
                image_url = item.get("image_url") if isinstance(item.get("image_url"), dict) else {}
                url = str(image_url.get("url") or "")
                if url.startswith("data:") and ";base64," in url:
                    media_type = url.split(":", 1)[1].split(";base64,", 1)[0] or "image/png"
                    data = url.split(";base64,", 1)[1]
                    blocks.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": data,
                            },
                        }
                    )
                elif url:
                    blocks.append({"type": "text", "text": f"[Image URL: {url}]"})
        return blocks or [{"type": "text", "text": ""}]
    return [{"type": "text", "text": str(content or "")}]


def _anthropic_tool_input_schema(tool: dict[str, Any]) -> dict[str, Any]:
    fn = tool.get("function") if isinstance(tool.get("function"), dict) else {}
    parameters = fn.get("parameters")
    return parameters if isinstance(parameters, dict) else {"type": "object", "properties": {}}


def _openai_tools_to_anthropic(tools: Optional[List[Dict[str, Any]]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tool in tools or []:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        out.append(
            {
                "name": name,
                "description": str(fn.get("description") or ""),
                "input_schema": _anthropic_tool_input_schema(tool),
            }
        )
    return out


def _openai_messages_to_anthropic(api_messages: List[Dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []

    for msg in api_messages:
        role = str(msg.get("role") or "").strip()
        content = msg.get("content", "")
        if role == _ANTHROPIC_SYSTEM_ROLE:
            if content:
                system_parts.append(str(content))
            continue

        if role == "tool":
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": str(msg.get("tool_call_id") or ""),
                            "content": str(content or ""),
                        }
                    ],
                }
            )
            continue

        anthropic_role = "assistant" if role == "assistant" else "user"
        blocks = _anthropic_content_blocks(content)
        if anthropic_role == "assistant":
            reasoning_items = msg.get(_REASONING_ITEMS_KEY)
            if isinstance(reasoning_items, list):
                native_blocks = [
                    copy.deepcopy(item)
                    for item in reasoning_items
                    if isinstance(item, dict)
                    and str(item.get("type") or "") in {"thinking", "redacted_thinking"}
                ]
                if native_blocks:
                    blocks = native_blocks + blocks
        tool_calls = msg.get("tool_calls")
        if anthropic_role == "assistant" and isinstance(tool_calls, list):
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                name = str(fn.get("name") or "").strip()
                if not name:
                    continue
                raw_args = fn.get("arguments")
                args: Any = {}
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args or "{}")
                    except Exception:
                        args = {}
                elif isinstance(raw_args, dict):
                    args = raw_args
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(call.get("id") or ""),
                        "name": name,
                        "input": args if isinstance(args, dict) else {},
                    }
                )
        messages.append({"role": anthropic_role, "content": blocks})

    return "\n\n".join(part for part in system_parts if part.strip()).strip(), messages


def _responses_text_from_content(content: Any) -> str:
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                if item:
                    parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "\n".join(parts).strip()
    if content is None:
        return ""
    return str(content)


def _responses_content_blocks(content: Any, *, role: str) -> Any:
    if not isinstance(content, list):
        return str(content or "")

    blocks: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            if item:
                blocks.append({"type": "input_text", "text": item})
            continue
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").strip()
        if item_type == "text":
            blocks.append({"type": "input_text", "text": str(item.get("text") or "")})
        elif item_type == "image_url" and role == "user":
            image_url = item.get("image_url") if isinstance(item.get("image_url"), dict) else {}
            url = str(image_url.get("url") or "").strip()
            if url:
                blocks.append({"type": "input_image", "image_url": url})

    if not blocks:
        return _responses_text_from_content(content)
    if role != "user" and not any(block.get("type") == "input_image" for block in blocks):
        return "\n".join(str(block.get("text") or "") for block in blocks if block.get("type") == "input_text").strip()
    return blocks


def _openai_tools_to_responses(tools: Optional[List[Dict[str, Any]]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tool in tools or []:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        parameters = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {"type": "object", "properties": {}}
        out.append(
            {
                "type": "function",
                "name": name,
                "description": str(fn.get("description") or ""),
                "parameters": parameters,
            }
        )
    return out


def _openai_messages_to_responses_input(api_messages: List[Dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    instructions_parts: list[str] = []
    input_items: list[dict[str, Any]] = []

    for msg in api_messages:
        role = str(msg.get("role") or "").strip()
        content = msg.get("content", "")
        if role == "system":
            text = _responses_text_from_content(content)
            if text:
                instructions_parts.append(text)
            continue

        if role == "tool":
            call_id = str(msg.get("tool_call_id") or "").strip()
            if call_id:
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": _responses_text_from_content(content),
                    }
                )
            continue

        if role == "assistant":
            reasoning_items = msg.get(_REASONING_ITEMS_KEY)
            if isinstance(reasoning_items, list):
                input_items.extend(
                    copy.deepcopy(item)
                    for item in reasoning_items
                    if isinstance(item, dict) and str(item.get("type") or "") == "reasoning"
                )
            text = _responses_text_from_content(content)
            if text:
                item = {"role": "assistant", "content": text}
                if msg.get('phase') in {'commentary', 'final_answer'}:
                    item['phase'] = msg['phase']
                input_items.append(item)

            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list):
                for call in tool_calls:
                    if not isinstance(call, dict):
                        continue
                    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                    name = str(fn.get("name") or "").strip()
                    if not name:
                        continue
                    raw_args = fn.get("arguments")
                    if isinstance(raw_args, dict):
                        arguments = json.dumps(raw_args, ensure_ascii=False, separators=(",", ":"))
                    else:
                        arguments = str(raw_args or "{}")
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": str(call.get("id") or ""),
                            "name": name,
                            "arguments": arguments,
                        }
                    )
            continue

        input_items.append(
            {
                "role": "user" if role not in {"user", "developer"} else role,
                "content": _responses_content_blocks(content, role="user"),
            }
        )

    return "\n\n".join(part for part in instructions_parts if part.strip()).strip(), input_items


def build_request_body(
    provider: Provider,
    conversation: Conversation,
    api_messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    *,
    llm_config: LLMConfig | None = None,
    reasoning_mode: str | None = None,
    token_budget: TokenBudget | None = None,
) -> Dict[str, Any]:
    request_cfg = llm_config or LLMConfig.from_conversation(conversation)
    payload_messages = list(api_messages)
    model = request_cfg.resolved_model()
    if not model:
        raise ValueError("No model selected for this conversation")
    profile = provider.effective_model_profile(model)
    wire_model = profile.model_id

    stream_enabled = request_cfg.resolved_stream(default=True)
    temperature = request_cfg.temperature
    if not isinstance(temperature, (int, float)):
        profile_temperature = getattr(profile, "default_temperature", None)
        temperature = float(profile_temperature) if isinstance(profile_temperature, (int, float)) else None
    top_p = request_cfg.top_p
    if not isinstance(top_p, (int, float)):
        profile_top_p = getattr(profile, "default_top_p", None)
        top_p = float(profile_top_p) if isinstance(profile_top_p, (int, float)) else None
    budget = token_budget or resolve_token_budget(
        conversation,
        provider=provider,
        model_id=model,
        llm_config=request_cfg,
    )
    max_tokens = int(budget.output_limit or 0)
    supports_tools = bool(getattr(profile, "supports_tools", True))
    request_tools = tools if supports_tools else []
    effective_reasoning_mode = (
        reasoning_mode if reasoning_mode is not None else getattr(request_cfg, "reasoning_mode", None)
    )

    if provider.is_anthropic_native:
        system_content, anthropic_messages = _openai_messages_to_anthropic(payload_messages)
        body: Dict[str, Any] = {
            "model": wire_model,
            "messages": anthropic_messages,
            "stream": stream_enabled,
        }
        if isinstance(temperature, (int, float)):
            body["temperature"] = float(temperature)
        if system_content:
            body["system"] = system_content
        anthropic_tools = _openai_tools_to_anthropic(request_tools)
        if anthropic_tools:
            body["tools"] = anthropic_tools
        if isinstance(top_p, (int, float)):
            body["top_p"] = float(top_p)
        apply_reasoning(
            body,
            profile=profile,
            mode=effective_reasoning_mode,
            openrouter=_is_openrouter_provider(provider),
        )
        _merge_request_extras(body, profile=profile)
        _write_output_limit(body, provider=provider, output_limit=max_tokens)
        return body

    if provider.is_openai_responses:
        instructions, responses_input = _openai_messages_to_responses_input(payload_messages)
        body = {
            "model": wire_model,
            "input": responses_input,
            "stream": stream_enabled,
        }
        if instructions:
            body["instructions"] = instructions
        responses_tools = _openai_tools_to_responses(request_tools)
        if responses_tools:
            body["tools"] = responses_tools
            body.setdefault("tool_choice", "auto")
        if isinstance(temperature, (int, float)):
            body["temperature"] = float(temperature)
        if isinstance(top_p, (int, float)):
            body["top_p"] = float(top_p)
        apply_reasoning(
            body,
            profile=profile,
            mode=effective_reasoning_mode,
        )
        _merge_request_extras(body, profile=profile)
        _write_output_limit(body, provider=provider, output_limit=max_tokens)
        if provider.auth_type == 'chatgpt':
            body['stream'] = True
            body['store'] = False
            body.setdefault('instructions', '')
            includes = body.get('include')
            body['include'] = list(dict.fromkeys([*(includes if isinstance(includes, list) else []), 'reasoning.encrypted_content']))
            for field in ('temperature', 'top_p', 'max_output_tokens', 'max_tokens', 'max_completion_tokens', 'previous_response_id'):
                body.pop(field, None)
        return body

    if provider.is_ollama_chat:
        body = {
            "model": wire_model,
            "messages": _openai_messages_to_ollama(payload_messages),
            "stream": stream_enabled,
        }
        options: dict[str, Any] = {}
        if isinstance(temperature, (int, float)):
            options["temperature"] = float(temperature)
        if isinstance(top_p, (int, float)):
            options["top_p"] = float(top_p)
        if options:
            body["options"] = options
        if request_tools:
            body["tools"] = request_tools
        apply_reasoning(
            body,
            profile=profile,
            mode=effective_reasoning_mode,
        )
        _merge_request_extras(body, profile=profile)
        _write_output_limit(body, provider=provider, output_limit=max_tokens)
        return body

    body = {
        "model": wire_model,
        "messages": [
            {key: value for key, value in message.items() if key != _REASONING_ITEMS_KEY}
            for message in payload_messages
        ],
        "stream": stream_enabled,
    }
    if isinstance(temperature, (int, float)):
        body["temperature"] = float(temperature)

    if request_tools:
        body["tools"] = request_tools
        # OpenAI-compatible default: let the model decide when to call tools.
        body.setdefault("tool_choice", "auto")

    if isinstance(top_p, (int, float)):
        body["top_p"] = float(top_p)

    apply_reasoning(
        body,
        profile=profile,
        mode=effective_reasoning_mode,
        openrouter=_is_openrouter_provider(provider),
    )
    _merge_request_extras(body, profile=profile)
    _write_output_limit(body, provider=provider, output_limit=max_tokens)
    if provider.auth_type == 'workbuddy':
        body['stream'] = True
        body['stream_options'] = {'include_usage': True}
    return body


def _merge_request_extras(body: Dict[str, Any], *, profile: Any) -> None:
    protected = {
        "model",
        "messages",
        "input",
        "instructions",
        "system",
        "tools",
        "stream",
    }
    extras = getattr(profile, "extra_body", None)
    if not isinstance(extras, dict):
        return
    for key, value in extras.items():
        name = str(key or "").strip()
        if not name:
            continue
        if name in protected:
            logger.warning("忽略 extra_body 中由运行时管理的结构字段: %s", name)
            continue
        if name in body:
            logger.warning("extra_body 覆盖请求字段: %s", name)
        body[name] = copy.deepcopy(value)


def _write_output_limit(
    body: Dict[str, Any],
    *,
    provider: Provider,
    output_limit: int,
) -> None:
    """Freeze the canonical output limit after model-level overrides."""

    value = max(0, int(output_limit or 0))
    if provider.is_openai_responses:
        _warn_output_limit_override("max_output_tokens", body.get("max_output_tokens"), value)
        if value > 0:
            body["max_output_tokens"] = value
        else:
            body.pop("max_output_tokens", None)
        return
    if provider.is_ollama_chat:
        options = body.get("options")
        if not isinstance(options, dict):
            options = {}
            body["options"] = options
        _warn_output_limit_override("options.num_predict", options.get("num_predict"), value)
        if value > 0:
            options["num_predict"] = value
        else:
            options.pop("num_predict", None)
        if not options:
            body.pop("options", None)
        return
    _warn_output_limit_override("max_tokens", body.get("max_tokens"), value)
    if value > 0:
        body["max_tokens"] = value
    else:
        body.pop("max_tokens", None)


def _warn_output_limit_override(field: str, configured: Any, effective: int) -> None:
    if configured is None:
        return
    try:
        configured_value = int(configured)
    except (TypeError, ValueError):
        configured_value = configured
    if configured_value == effective:
        logger.warning("extra_body 重复请求字段，运行时预算保持该值: %s", field)
        return
    logger.warning(
        "extra_body 请求字段已按运行时预算归一: %s (%r -> %d)",
        field,
        configured,
        effective,
    )
