from __future__ import annotations

import logging
import json
import os
from typing import Any, Dict, List, Optional

from models.conversation import Conversation, Message, normalize_tool_result
from models.provider import Provider
from core.content.attachments import encode_image_file_to_data_url
from core.prompts.system import PromptManager
from core.prompts.context_assembler import build_context_messages
from core.prompts.history import apply_context_window
from core.config import AppConfig, load_app_config
from core.llm.llm_config import LLMConfig

logger = logging.getLogger(__name__)
_ANTHROPIC_SYSTEM_ROLE = "system"
_RUNTIME_ERROR_PREFIXES = (
    "http 错误",
    "模型调用失败：",
    "错误:",
    "error sending message:",
)


def _normalize_image_url(image: str) -> str:
    if image.startswith("data:") or image.startswith(("http://", "https://")):
        return image
    return encode_image_file_to_data_url(image) or ""


def _build_multimodal_content(text_content: Any, images: list[str], provider: Provider) -> Any:
    if not images or not provider.supports_vision:
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


def _build_message_content(msg: Message, provider: Provider) -> Any:
    text_content = msg.summary if msg.summary else msg.content
    return _build_multimodal_content(text_content, list(getattr(msg, "images", []) or []), provider)


def _tool_result_content_for_api(result: Any) -> str:
    payload = normalize_tool_result(result)
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    try:
        return json.dumps(content, ensure_ascii=False)
    except Exception:
        return str(content)


def _provider_declares_reasoning_support(provider: Provider) -> bool:
    request_format = getattr(provider, "request_format", None)
    if isinstance(request_format, dict):
        for key in ("thinking", "reasoning", "reasoning_content", "thinking_content"):
            if key in request_format:
                return True
    return bool(getattr(provider, "supports_thinking", False))


def _conversation_show_thinking(conversation: Conversation | None) -> bool | None:
    if conversation is None:
        return None
    settings = getattr(conversation, "settings", {}) or {}
    value = settings.get("show_thinking")
    return value if isinstance(value, bool) else None


def _assistant_has_reasoning(msg: Message) -> bool:
    if bool(str(getattr(msg, "thinking", "") or "").strip()):
        return True
    metadata = getattr(msg, "metadata", {}) or {}
    return isinstance(metadata, dict) and bool(metadata.get("thinking_present"))


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
    conversation: Conversation | None = None,
) -> bool:
    if provider.is_anthropic_native:
        return False
    if any(m.role == "assistant" and _assistant_has_reasoning(m) for m in messages):
        return True
    show_thinking = _conversation_show_thinking(conversation)
    return bool(show_thinking) and _provider_declares_reasoning_support(provider)


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


def _tool_call_summary_lines(tool_calls: Any) -> list[str]:
    lines: list[str] = []
    for tool_call in tool_calls or []:
        if not isinstance(tool_call, dict):
            continue
        func = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
        name = str(func.get("name") or "unknown_tool").strip() or "unknown_tool"
        summary = str(tool_call.get("result_summary") or "").strip()
        result = _tool_result_content_for_api(tool_call.get("result")).strip()
        if not summary and result:
            summary = result.splitlines()[0].strip()[:220]
        if summary:
            lines.append(f"- {name}: {summary}")
        else:
            lines.append(f"- {name}")
    return lines


def _recover_assistant_as_user(msg: Message) -> Message | None:
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

    tool_summaries = _tool_call_summary_lines(getattr(msg, "tool_calls", None))
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


def _tool_call_has_result(tool_call: Any) -> bool:
    if not isinstance(tool_call, dict):
        return False
    if tool_call.get("result") is not None:
        return True
    if tool_call.get("result_summary"):
        return True
    return bool(tool_call.get("result_images"))


def _filter_tool_calls_without_result(msg: Message) -> Message | None:
    if not msg.tool_calls:
        return msg
    kept = [tc for tc in msg.tool_calls if _tool_call_has_result(tc)]
    if not kept:
        return _recover_assistant_as_user(msg)
    if len(kept) == len(msg.tool_calls):
        return msg
    clone = Message.from_dict(msg.to_dict())
    clone.tool_calls = kept
    return clone


def _sanitize_reasoning_history(
    messages: List[Message],
    provider: Provider,
    *,
    conversation: Conversation | None = None,
) -> List[Message]:
    if not _should_replay_reasoning(messages, provider, conversation=conversation):
        return messages

    sanitized: List[Message] = []
    for msg in messages:
        if msg.role == "assistant" and not _assistant_has_reasoning(msg):
            if _is_runtime_error_message(msg):
                continue
            recovered = _recover_assistant_as_user(msg)
            if recovered is not None:
                sanitized.append(recovered)
            continue

        if msg.role == "tool":
            continue

        sanitized.append(msg)

    return sanitized


def select_base_messages(conversation: Conversation, *, app_config: AppConfig | None = None) -> List[Message]:
    cfg = app_config
    if cfg is None:
        try:
            cfg = load_app_config()
        except Exception:
            cfg = AppConfig()

    keep_last_turns = int(
        getattr(getattr(cfg, "context", None), "compression_policy", None).keep_last_n
        if getattr(getattr(cfg, "context", None), "compression_policy", None)
        else 3
    )
    messages = build_context_messages(
        conversation,
        app_config=cfg,
        keep_last_turns=keep_last_turns,
        default_work_dir=getattr(conversation, "work_dir", ".") or ".",
    )

    synthetic_prefix: List[Message] = []
    recent_history = list(messages)
    while recent_history and bool(getattr(recent_history[0], "metadata", {}).get("synthetic")):
        synthetic_prefix.append(recent_history.pop(0))

    settings = conversation.settings or {}
    max_ctx = settings.get("max_context_messages")
    if isinstance(max_ctx, int) and max_ctx > 0:
        return synthetic_prefix + apply_context_window(recent_history, max_ctx)

    default_max_ctx = int(getattr(getattr(cfg, "context", None), "default_max_context_messages", 0) or 0)
    if default_max_ctx > 0:
        return synthetic_prefix + apply_context_window(recent_history, default_max_ctx)
                
    return messages


def build_api_messages(
    messages: List[Message],
    provider: Provider,
    *,
    conversation: Conversation | None = None,
) -> List[Dict[str, Any]]:
    messages = _sanitize_reasoning_history(messages, provider, conversation=conversation)
    api_messages: List[Dict[str, Any]] = []

    # Safety check: warn if no user messages in input
    has_user = any(m.role == "user" for m in messages)
    if not has_user:
        logger.warning("build_api_messages: no user messages found — context may be corrupted")

    for msg in messages:
        if msg.role == "tool":
            continue

        message_payload = {"role": msg.role, "content": _build_message_content(msg, provider)}

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
                    result = _tool_result_content_for_api(result)
                if result_images:
                    result = _build_multimodal_content(result, result_images, provider)
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
                    "content": message_payload.get("content"),
                    "tool_calls": [tc["clean"] for tc in tool_calls_with_results],
                }

                if _assistant_has_reasoning(msg):
                    key = msg.metadata.get("thinking_key") or "reasoning_content"
                    assistant_payload[key] = msg.thinking or ""

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

        if msg.role == "assistant" and _assistant_has_reasoning(msg):
            key = msg.metadata.get("thinking_key") or "reasoning_content"
            message_payload[key] = msg.thinking or ""

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
                        import json

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
            text = _responses_text_from_content(content)
            if text:
                input_items.append({"role": "assistant", "content": text})

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
    app_config: AppConfig | None = None,
    llm_config: LLMConfig | None = None,
) -> Dict[str, Any]:
    request_cfg = llm_config or LLMConfig.from_conversation(conversation)
    payload_messages = list(api_messages)

    stream_enabled = request_cfg.resolved_stream(default=True)
    temperature = request_cfg.temperature
    if not isinstance(temperature, (int, float)):
        temperature = 0.7
    top_p = request_cfg.top_p
    max_tokens = int(request_cfg.max_tokens or 0)

    # Respect a pre-assembled system message if the caller already prepared one.
    system_msg_index = -1
    for i, msg in enumerate(payload_messages):
        if msg.get("role") == "system":
            system_msg_index = i
            break

    if system_msg_index < 0:
        if request_cfg.system_prompt_override.strip():
            system_prompt_content = request_cfg.system_prompt_override.strip()
        else:
            work_dir = getattr(conversation, "work_dir", ".")
            prompt_manager = PromptManager(work_dir)
            cfg = app_config
            if cfg is None:
                try:
                    cfg = load_app_config()
                except Exception:
                    cfg = AppConfig()

            system_prompt_content = prompt_manager.get_system_prompt(
                conversation,
                tools or [],
                provider,
                app_config=cfg,
            )

        # Insert new system message at the beginning
        payload_messages.insert(0, {
            "role": "system",
            "content": system_prompt_content
        })
    
    if provider.is_anthropic_native:
        system_content, anthropic_messages = _openai_messages_to_anthropic(payload_messages)
        body: Dict[str, Any] = {
            "model": request_cfg.resolved_model(provider),
            "messages": anthropic_messages,
            "temperature": temperature,
            "stream": stream_enabled,
            "max_tokens": max_tokens if max_tokens > 0 else 4096,
        }
        if system_content:
            body["system"] = system_content
        anthropic_tools = _openai_tools_to_anthropic(tools)
        if anthropic_tools:
            body["tools"] = anthropic_tools
        if isinstance(top_p, (int, float)):
            body["top_p"] = float(top_p)
        _merge_request_extras(body, request_cfg=request_cfg, provider=provider)
        return body

    if provider.is_openai_responses:
        instructions, responses_input = _openai_messages_to_responses_input(payload_messages)
        body = {
            "model": request_cfg.resolved_model(provider),
            "input": responses_input,
            "stream": stream_enabled,
        }
        if instructions:
            body["instructions"] = instructions
        if max_tokens > 0:
            body["max_output_tokens"] = max_tokens
        responses_tools = _openai_tools_to_responses(tools)
        if responses_tools:
            body["tools"] = responses_tools
            body.setdefault("tool_choice", "auto")
        if isinstance(temperature, (int, float)):
            body["temperature"] = float(temperature)
        if isinstance(top_p, (int, float)):
            body["top_p"] = float(top_p)
        _merge_request_extras(body, request_cfg=request_cfg, provider=provider)
        return body

    body = {
        "model": request_cfg.resolved_model(provider),
        "messages": payload_messages,
        "temperature": temperature,
        "stream": stream_enabled,
    }

    if max_tokens > 0:
        body["max_tokens"] = max_tokens
    
    if tools:
        body["tools"] = tools
        # OpenAI-compatible default: let the model decide when to call tools.
        body.setdefault("tool_choice", "auto")

    if isinstance(top_p, (int, float)):
        body["top_p"] = float(top_p)

    _merge_request_extras(body, request_cfg=request_cfg, provider=provider)
    return body


def _merge_request_extras(body: Dict[str, Any], *, request_cfg: LLMConfig, provider: Provider) -> None:
    extras = getattr(request_cfg, "extras", None)
    if isinstance(extras, dict) and extras:
        protected = {"model", "messages", "input", "instructions"}
        for k, v in extras.items():
            if k in protected or k in body:
                continue
            body[k] = v

    # Provider-level extras are merged (without overriding core keys).
    provider_extras = getattr(provider, "request_format", None)
    if isinstance(provider_extras, dict) and provider_extras:
        protected = {"model", "messages", "input", "instructions"}
        for k, v in provider_extras.items():
            if k in protected or k in body:
                continue
            body[k] = v
