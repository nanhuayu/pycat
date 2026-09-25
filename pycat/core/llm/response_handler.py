"""Response parsing for LLM API calls.

Extracts non-streaming and streaming response handling from
``LLMClient.send_message`` so that ``client.py`` stays focused
on orchestration.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

import httpx

from pycat.core.llm.http_utils import (
    format_http_error,
    iter_sse_data_lines,
    parse_json_safely,
    parse_sse_json,
    pretty_json,
    read_response_bytes,
)
from pycat.core.llm.ollama_codec import parse_message as parse_ollama_message
from pycat.core.llm.reasoning import CHAT_REASONING_CODEC, normalize_reasoning_codec
from pycat.core.llm.thinking_parser import ThinkingStreamParser
from pycat.core.llm.token_budget import estimate_tokens
from pycat.models.conversation import Message

logger = logging.getLogger(__name__)

# Fields that may contain thinking / reasoning content across providers
THINKING_KEYS = [
    "reasoning_content", "thinking", "reasoning",
    "thinking_content", "thoughts", "thought",
]


def _mark_reasoning_seen(
    *,
    key: str,
    value: Any,
    detected_thinking_key: str,
) -> tuple[str, str, bool]:
    """Return normalized reasoning text plus the detected key.

    Some OpenAI-compatible reasoning models emit ``reasoning_content`` as an
    empty string before tool calls. That empty field still has to be replayed
    in later requests, so callers must distinguish "field was present but
    empty" from "field never existed".
    """

    text = "" if value is None else str(value)
    return text, key or detected_thinking_key, True


_OUTPUT_LIMIT_REASONS = frozenset(
    {
        "length",
        "max_tokens",
        "max_output_tokens",
        "max_completion_tokens",
        "output_limit",
        "token_limit",
    }
)

_INCOMPLETE_RESPONSE_REASONS = frozenset(
    {
        "content_filter",
        "safety",
        "cancelled",
        "canceled",
    }
)

_RUNTIME_ERROR_REASONS = frozenset(
    {
        "failed",
        "error",
    }
)


def _metadata_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_metadata_int(payload: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = _metadata_int(payload.get(key))
        if value is not None:
            return value
    return None


def _response_metadata_from_payload(
    payload: dict[str, Any],
    *,
    response_format: str,
) -> dict[str, Any]:
    """Normalize completion facts exposed by provider response envelopes.

    The runtime only needs a small portable observation contract.  It keeps
    provider wire shapes at this boundary instead of making the Agent loop,
    debug trace, and Channel delivery infer termination independently.
    """

    if not isinstance(payload, dict):
        return {}

    event_type = str(payload.get("type") or "").strip().lower()
    nested_message = payload.get("message")
    if event_type == "message_start" and isinstance(nested_message, dict):
        return _response_metadata_from_payload(
            nested_message,
            response_format=response_format,
        )
    nested_response = payload.get("response")
    if event_type.startswith("response.") and isinstance(nested_response, dict):
        observed = _response_metadata_from_payload(
            nested_response,
            response_format=response_format,
        )
        if event_type == "response.incomplete":
            observed["incomplete"] = True
            observed.setdefault("finish_reason", "incomplete")
            observed.setdefault("incomplete_reason", "incomplete_response")
        elif event_type in {"response.cancelled", "response.canceled"}:
            observed["incomplete"] = True
            observed.setdefault("finish_reason", "cancelled")
            observed.setdefault("incomplete_reason", "cancelled")
        elif event_type == "response.failed":
            observed["runtime_error"] = True
            observed.setdefault("finish_reason", "failed")
        return observed

    # Some compatible Responses implementations omit the nested ``response``
    # envelope on terminal events.  Preserve the terminal state instead of
    # letting the event look like a clean EOF.
    if event_type == "response.incomplete":
        return {
            "incomplete": True,
            "runtime_error": False,
            "finish_reason": "incomplete",
            "incomplete_reason": "incomplete_response",
        }
    if event_type in {"response.cancelled", "response.canceled"}:
        return {
            "incomplete": True,
            "runtime_error": False,
            "finish_reason": "cancelled",
            "incomplete_reason": "cancelled",
        }
    if event_type == "response.failed":
        return {
            "incomplete": False,
            "runtime_error": True,
            "finish_reason": "failed",
        }

    normalized_format = str(response_format or "").strip().lower()
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    finish_reason = ""
    status = str(payload.get("status") or "").strip().lower()
    if event_type == "response.incomplete" and not status:
        status = "incomplete"

    if normalized_format in {"responses", "openai_responses"}:
        if status == "incomplete":
            details = payload.get("incomplete_details")
            details = details if isinstance(details, dict) else {}
            finish_reason = str(details.get("reason") or "incomplete").strip().lower()
        elif status:
            finish_reason = status
    elif normalized_format == "ollama_chat":
        finish_reason = str(payload.get("done_reason") or "").strip().lower()
    elif isinstance(payload.get("content"), list):
        finish_reason = str(payload.get("stop_reason") or "").strip().lower()
    elif event_type == "message_delta":
        delta = payload.get("delta") if isinstance(payload.get("delta"), dict) else {}
        finish_reason = str(delta.get("stop_reason") or "").strip().lower()
    else:
        choices = payload.get("choices") if isinstance(payload.get("choices"), list) else []
        if choices and isinstance(choices[0], dict):
            finish_reason = str(choices[0].get("finish_reason") or "").strip().lower()

    # A few OpenAI-compatible gateways put the terminal state on the envelope
    # instead of ``choices[0].finish_reason``.  Preserve only states with a
    # defined runtime meaning; ordinary progress/status labels must not leak
    # into the portable metadata contract.
    if not finish_reason and status in (
        _OUTPUT_LIMIT_REASONS
        | _INCOMPLETE_RESPONSE_REASONS
        | _RUNTIME_ERROR_REASONS
        | {"incomplete"}
    ):
        finish_reason = status

    prompt_tokens = _first_metadata_int(usage, "prompt_tokens", "input_tokens")
    completion_tokens = _first_metadata_int(usage, "completion_tokens", "output_tokens")
    if normalized_format == "ollama_chat":
        prompt_tokens = _metadata_int(payload.get("prompt_eval_count"))
        completion_tokens = _metadata_int(payload.get("eval_count"))

    details = (
        usage.get("completion_tokens_details")
        if isinstance(usage.get("completion_tokens_details"), dict)
        else usage.get("output_tokens_details")
        if isinstance(usage.get("output_tokens_details"), dict)
        else {}
    )
    reasoning_tokens = _first_metadata_int(
        usage,
        "reasoning_tokens",
    )
    if reasoning_tokens is None:
        reasoning_tokens = _first_metadata_int(details, "reasoning_tokens")

    # Do not derive total_tokens here.  Streaming providers may expose input
    # and output usage in different events (Anthropic message_start/delta).
    # Derivation is deferred until the stream is complete so an early output
    # count cannot become a stale total.
    total_tokens = _first_metadata_int(usage, "total_tokens")

    incomplete = (
        status in {"incomplete", "cancelled", "canceled"}
        or finish_reason in _OUTPUT_LIMIT_REASONS
        or finish_reason in _INCOMPLETE_RESPONSE_REASONS
    )
    runtime_error = status in _RUNTIME_ERROR_REASONS or finish_reason in _RUNTIME_ERROR_REASONS
    observed: dict[str, Any] = {
        "incomplete": bool(incomplete),
        "runtime_error": bool(runtime_error),
    }
    if normalized_format in {'responses', 'openai_responses'}:
        items = payload.get('output') or []
        if isinstance(payload.get('item'), dict):
            items = [payload['item']]
        for item in items:
            if isinstance(item, dict) and item.get('type') == 'message' and item.get('phase') in {'commentary', 'final_answer'}:
                observed['responses_phase'] = item['phase']
    if finish_reason:
        observed["finish_reason"] = finish_reason
    if prompt_tokens is not None:
        observed["prompt_tokens"] = prompt_tokens
    if completion_tokens is not None:
        observed["completion_tokens"] = completion_tokens
    if reasoning_tokens is not None:
        observed["reasoning_tokens"] = reasoning_tokens
    if total_tokens is not None:
        observed["total_tokens"] = total_tokens
    if incomplete:
        if finish_reason in _OUTPUT_LIMIT_REASONS:
            observed["incomplete_reason"] = "output_limit"
        else:
            observed["incomplete_reason"] = finish_reason or "incomplete_response"
    return observed


def _merge_response_metadata(target: dict[str, Any], observed: dict[str, Any]) -> None:
    if not observed:
        return
    for key in (
        "finish_reason",
        "responses_phase",
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "total_tokens",
        "incomplete_reason",
    ):
        if observed.get(key) is not None and observed.get(key) != "":
            target[key] = observed[key]
    if observed.get("runtime_error"):
        target["runtime_error"] = True
    else:
        target.setdefault("runtime_error", False)
    if observed.get("incomplete"):
        target["incomplete"] = True
    else:
        target.setdefault("incomplete", False)


def _complete_response_metadata(metadata: dict[str, Any]) -> None:
    """Fill a missing total from the final prompt/completion counts."""

    if not isinstance(metadata, dict) or metadata.get("total_tokens") is not None:
        return
    prompt_tokens = _metadata_int(metadata.get("prompt_tokens"))
    completion_tokens = _metadata_int(metadata.get("completion_tokens"))
    if prompt_tokens is not None and completion_tokens is not None:
        metadata["total_tokens"] = prompt_tokens + completion_tokens


def _response_failure_detail(payload: Any) -> Any:
    """Return the most useful bounded provider error payload."""

    if not isinstance(payload, dict):
        return payload
    error = payload.get("error")
    if error is not None:
        return error
    response = payload.get("response")
    if isinstance(response, dict) and response.get("error") is not None:
        return response["error"]
    return response if isinstance(response, dict) else payload


def _format_provider_failure(payload: Any, *, source: str) -> str:
    return f"接口返回错误（{source}）：\n" + pretty_json(_response_failure_detail(payload))


def _finalize_message_metadata(
    msg: Message,
    *,
    detected_thinking_key: str,
    thinking_present: bool,
    show_thinking: bool,
    runtime_error: bool = False,
    http_status: int | None = None,
    reasoning_state: dict[str, Any] | None = None,
    response_metadata: dict[str, Any] | None = None,
) -> Message:
    msg.metadata["thinking_key"] = detected_thinking_key
    if thinking_present:
        msg.metadata["thinking_present"] = True
        if not show_thinking:
            msg.metadata["thinking_hidden"] = True
    if runtime_error:
        msg.metadata["runtime_error"] = True
        if http_status is not None:
            msg.metadata["http_status"] = http_status
    if reasoning_state:
        msg.metadata["reasoning_state"] = copy.deepcopy(reasoning_state)
    if response_metadata is not None:
        for key in (
            "finish_reason",
            "responses_phase",
            "prompt_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "total_tokens",
            "incomplete_reason",
        ):
            value = response_metadata.get(key)
            if value is not None and value != "":
                msg.metadata[key] = value
        msg.metadata["incomplete"] = bool(response_metadata.get("incomplete", False))
        if response_metadata.get("runtime_error"):
            msg.metadata["runtime_error"] = True
    return msg


def _json_dumps_compact(value: Any) -> str:
    try:
        return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return "{}"


def _parse_anthropic_content_blocks(
    payload: Dict[str, Any],
) -> tuple[str, str, List[Dict[str, Any]], List[Dict[str, Any]], int]:
    """Return text, thinking, native reasoning blocks, tool calls, and usage."""
    text_parts: List[str] = []
    thinking_parts: List[str] = []
    reasoning_blocks: List[Dict[str, Any]] = []
    tool_calls: List[Dict[str, Any]] = []

    for block in payload.get("content", []) or []:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "").strip()
        if block_type == "text":
            text = str(block.get("text") or "")
            if text:
                text_parts.append(text)
        elif block_type in {"thinking", "redacted_thinking"}:
            reasoning_blocks.append(copy.deepcopy(block))
            if block_type == "thinking":
                thinking = str(block.get("thinking") or "")
                if thinking:
                    thinking_parts.append(thinking)
        elif block_type == "tool_use":
            name = str(block.get("name") or "").strip()
            if not name:
                continue
            tool_calls.append(
                {
                    "id": str(block.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": _json_dumps_compact(block.get("input") or {}),
                    },
                }
            )

    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    tokens = 0
    for key in ("output_tokens", "input_tokens"):
        try:
            tokens += int(usage.get(key) or 0)
        except Exception:
            continue

    return "".join(text_parts), "".join(thinking_parts), reasoning_blocks, tool_calls, tokens


def _responses_reasoning_items(payload: Dict[str, Any]) -> list[dict[str, Any]]:
    output = payload.get("output") if isinstance(payload.get("output"), list) else []
    return [
        copy.deepcopy(item)
        for item in output
        if isinstance(item, dict) and str(item.get("type") or "") == "reasoning"
    ]


def _chat_reasoning_details(message: Any) -> list[dict[str, Any]]:
    if not isinstance(message, dict) or not isinstance(message.get("reasoning_details"), list):
        return []
    return [copy.deepcopy(item) for item in message["reasoning_details"] if isinstance(item, dict)]


def _merge_chat_reasoning_details(
    buffer: dict[int, dict[str, Any]],
    details: Any,
) -> None:
    if not isinstance(details, list):
        return
    for position, raw in enumerate(details):
        if not isinstance(raw, dict):
            continue
        try:
            index = int(raw.get("index", position))
        except (TypeError, ValueError):
            index = position
        target = buffer.setdefault(index, {})
        for key, value in raw.items():
            if key == "index":
                target[key] = value
            elif key in {"text", "data", "summary", "signature"} and isinstance(value, str):
                previous = target.get(key)
                if not isinstance(previous, str) or not previous:
                    target[key] = value
                elif not value or value == previous or previous.endswith(value):
                    continue
                elif value.startswith(previous):
                    target[key] = value
                else:
                    target[key] = previous + value
            else:
                target[key] = copy.deepcopy(value)


def _responses_usage_tokens(payload: Dict[str, Any]) -> int:
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    total = usage.get("total_tokens")
    try:
        if total is not None:
            return int(total)
    except Exception:
        pass

    tokens = 0
    for key in ("input_tokens", "output_tokens"):
        try:
            tokens += int(usage.get(key) or 0)
        except Exception:
            continue
    return tokens


def _responses_arguments_to_string(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return "{}"


def _responses_tool_call_key(payload: Dict[str, Any], current_index: Optional[int], current_item_id: str, fallback_size: int) -> str:
    if payload.get("output_index") is not None:
        return str(payload.get("output_index"))
    item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
    item_id = str(item.get("id") or payload.get("item_id") or current_item_id or "").strip()
    if item_id:
        return item_id
    if current_index is not None:
        return str(current_index)
    return str(fallback_size)


def _ensure_responses_tool_call(buffer: Dict[str, dict], key: str, *, call_id: str = "", name: str = "") -> dict:
    tcb = buffer.setdefault(
        key,
        {
            "id": str(call_id or ""),
            "type": "function",
            "function": {"name": str(name or ""), "arguments": ""},
        },
    )
    if call_id:
        tcb["id"] = str(call_id)
    if name:
        tcb["function"]["name"] = str(name)
    return tcb


def _sync_responses_function_call_item(buffer: Dict[str, dict], key: str, item: Dict[str, Any]) -> None:
    if not isinstance(item, dict) or item.get("type") != "function_call":
        return
    tcb = _ensure_responses_tool_call(
        buffer,
        key,
        call_id=str(item.get("call_id") or item.get("id") or ""),
        name=str(item.get("name") or ""),
    )
    if item.get("arguments") is not None:
        tcb["function"]["arguments"] = _responses_arguments_to_string(item.get("arguments"))


def _parse_responses_payload(payload: Dict[str, Any]) -> tuple[str, str, List[Dict[str, Any]], int, str]:
    """Return text, reasoning, OpenAI-style tool calls, token usage, thinking key."""
    text_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    detected_thinking_key = "reasoning"

    direct_text = payload.get("output_text")
    if isinstance(direct_text, str) and direct_text:
        text_parts.append(direct_text)

    output = payload.get("output") if isinstance(payload.get("output"), list) else []
    for item in output:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").strip()
        if item_type in {"message", "assistant_message"}:
            content_blocks = item.get("content") if isinstance(item.get("content"), list) else []
            for block in content_blocks:
                if not isinstance(block, dict):
                    continue
                block_type = str(block.get("type") or "").strip()
                if block_type in {"output_text", "text", "input_text"}:
                    text = str(block.get("text") or "")
                    if text:
                        text_parts.append(text)
                elif block_type in {"reasoning_text", "summary_text"}:
                    text = str(block.get("text") or block.get("summary") or "")
                    if text:
                        reasoning_parts.append(text)
                        detected_thinking_key = "reasoning"
        elif item_type == "function_call":
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            call_id = str(item.get("call_id") or item.get("id") or "").strip()
            tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": _responses_arguments_to_string(item.get("arguments")),
                    },
                }
            )
        elif item_type == "reasoning":
            summary = item.get("summary")
            if isinstance(summary, list):
                for block in summary:
                    if isinstance(block, dict):
                        text = str(block.get("text") or block.get("summary") or "")
                        if text:
                            reasoning_parts.append(text)
                    elif isinstance(block, str) and block:
                        reasoning_parts.append(block)
            elif isinstance(summary, str) and summary:
                reasoning_parts.append(summary)
            content = item.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        text = str(block.get("text") or block.get("summary") or "")
                        if text:
                            reasoning_parts.append(text)
            detected_thinking_key = "reasoning"

    if not text_parts:
        choices = payload.get("choices") if isinstance(payload.get("choices"), list) else []
        if choices:
            msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
            content = str(msg.get("content") or "") if isinstance(msg, dict) else ""
            if content:
                text_parts.append(content)

    return "".join(text_parts), "".join(reasoning_parts), tool_calls, _responses_usage_tokens(payload), detected_thinking_key


def parse_non_stream_response(
    resp: httpx.Response,
    *,
    thinking_parser: ThinkingStreamParser,
    show_thinking: bool,
    response_format: str = "chat",
    reasoning_codec: str = "",
    on_token: Optional[Callable[[str], None]],
    start_time: float,
) -> Message:
    """Parse a non-streaming (``stream=false``) HTTP response into a ``Message``."""
    # Keep direct parser callers on the same canonical contract as LLMClient;
    # legacy aliases must never leak into persisted reasoning_state.  Preserve
    # an omitted codec as an empty sentinel so the response-format fallback
    # (Anthropic/Responses) remains active for low-level callers.
    raw_reasoning_codec = str(reasoning_codec or "").strip()
    reasoning_codec = normalize_reasoning_codec(raw_reasoning_codec) if raw_reasoning_codec else ""
    response_content = ""
    thinking_content = ""
    tokens_used = 0
    response_tool_calls: Optional[List[Dict[str, Any]]] = None
    detected_thinking_key = "reasoning_content"
    thinking_present = False
    reasoning_state: dict[str, Any] | None = None
    runtime_error = False
    http_status: int | None = None
    response_metadata: dict[str, Any] | None = None

    if resp.status_code >= 400:
        runtime_error = True
        http_status = int(resp.status_code)
        payload = None
        try:
            payload = resp.json()
        except Exception:
            payload = None
        text = ""
        try:
            text = (resp.text or "").strip()
        except Exception:
            text = ""
        response_content = format_http_error(resp.status_code, payload, text)
    else:
        payload = resp.json()
        response_metadata = {}
        if isinstance(payload, dict):
            _merge_response_metadata(
                response_metadata,
                _response_metadata_from_payload(payload, response_format=response_format),
            )
        if response_format in {"responses", "openai_responses"} and isinstance(payload, dict):
            content, thinking, tool_calls, tokens_used, detected_thinking_key = _parse_responses_payload(payload)
            reasoning_items = _responses_reasoning_items(payload)
            if reasoning_items:
                reasoning_state = {
                    "codec": reasoning_codec or "responses_effort",
                    "items": reasoning_items,
                }
            visible, embedded_thinking = thinking_parser.feed(content)
            response_content += visible
            if embedded_thinking:
                thinking_present = True
                thinking_content += embedded_thinking
            if thinking:
                thinking_present = True
                thinking_content += thinking
            if tool_calls:
                response_tool_calls = tool_calls
        elif response_format == "ollama_chat" and isinstance(payload, dict):
            content, thinking, tool_calls, tokens_used = parse_ollama_message(payload)
            visible, embedded_thinking = thinking_parser.feed(content)
            response_content += visible
            if embedded_thinking:
                thinking_present = True
                thinking_content += embedded_thinking
            if thinking:
                detected_thinking_key = "thinking"
                thinking_present = True
                thinking_content += thinking
            if tool_calls:
                response_tool_calls = tool_calls
        elif isinstance(payload, dict) and isinstance(payload.get("content"), list):
            content, thinking, reasoning_blocks, tool_calls, tokens_used = _parse_anthropic_content_blocks(payload)
            if reasoning_blocks:
                reasoning_state = {
                    "codec": reasoning_codec or "anthropic_adaptive",
                    "items": reasoning_blocks,
                }
            visible, embedded_thinking = thinking_parser.feed(content)
            response_content += visible
            if embedded_thinking:
                thinking_present = True
                thinking_content += embedded_thinking
            if thinking:
                detected_thinking_key = "thinking"
                thinking_present = True
                thinking_content += thinking
            if tool_calls:
                response_tool_calls = tool_calls
        else:
            choices = payload.get("choices", []) if isinstance(payload, dict) else []
            if choices:
                msg = choices[0].get("message", {}) or {}
                if isinstance(msg, dict):
                    tcs = msg.get("tool_calls")
                    if isinstance(tcs, list) and tcs:
                        response_tool_calls = tcs
                    reasoning_details = _chat_reasoning_details(msg)
                    if normalize_reasoning_codec(reasoning_codec) == CHAT_REASONING_CODEC and reasoning_details:
                        reasoning_state = {
                            "codec": CHAT_REASONING_CODEC,
                            "items": reasoning_details,
                        }
                content = msg.get("content", "") or ""
                visible, embedded_thinking = thinking_parser.feed(content)
                response_content += visible

                thinking = ""
                for key in THINKING_KEYS:
                    if key in msg and msg.get(key) is not None:
                        thinking, detected_thinking_key, thinking_present = _mark_reasoning_seen(
                            key=key,
                            value=msg.get(key),
                            detected_thinking_key=detected_thinking_key,
                        )
                        break

                if embedded_thinking:
                    thinking_present = True
                    thinking_content += embedded_thinking
                if thinking:
                    thinking_content += thinking
            else:
                response_content = pretty_json(payload)

        if response_metadata and response_metadata.get("runtime_error"):
            runtime_error = True
            # A provider-declared failure is not an assistant answer.  Replace
            # any best-effort parser output with a bounded diagnostic so the
            # Agent loop cannot mistake it for a successful turn.
            response_content = _format_provider_failure(
                payload,
                source=response_format or "provider",
            )

    if on_token and response_content:
        on_token(response_content)

    response_time_ms = int((time.time() - start_time) * 1000)
    _complete_response_metadata(response_metadata or {})
    observed_total = _metadata_int((response_metadata or {}).get("total_tokens"))
    if observed_total is not None:
        tokens_used = observed_total
    elif tokens_used == 0 and response_content:
        tokens_used = estimate_tokens(response_content)

    msg = Message(
        role="assistant",
        content=response_content,
        thinking=thinking_content if thinking_content else None,
        tool_calls=response_tool_calls if response_tool_calls else None,
        tokens=tokens_used,
        response_time_ms=response_time_ms,
    )
    return _finalize_message_metadata(
        msg,
        detected_thinking_key=detected_thinking_key,
        thinking_present=thinking_present,
        show_thinking=show_thinking,
        runtime_error=runtime_error,
        http_status=http_status,
        reasoning_state=reasoning_state,
        response_metadata=response_metadata,
    )


async def parse_stream_response(
    response: httpx.Response,
    *,
    thinking_parser: ThinkingStreamParser,
    show_thinking: bool,
    response_format: str = "chat",
    reasoning_codec: str = "",
    on_token: Optional[Callable[[str], None]],
    on_thinking: Optional[Callable[[str], None]],
    cancel_event,
    log_fp,
    start_time: float,
) -> Message:
    """Consume an SSE stream and return the final ``Message``."""
    raw_reasoning_codec = str(reasoning_codec or "").strip()
    reasoning_codec = normalize_reasoning_codec(raw_reasoning_codec) if raw_reasoning_codec else ""
    response_content = ""
    thinking_content = ""
    tokens_used = 0
    response_tool_calls: Optional[List[Dict[str, Any]]] = None
    detected_thinking_key = "reasoning_content"
    thinking_present = False
    reasoning_state: dict[str, Any] | None = None
    runtime_error = False
    http_status: int | None = None
    response_metadata: dict[str, Any] = {}
    failure_payload: Any = None

    # HTTP error (non-2xx with streaming client)
    if response.status_code >= 400:
        runtime_error = True
        http_status = int(response.status_code)
        raw = await read_response_bytes(response)
        text = ""
        payload = None
        if raw:
            try:
                text = raw.decode("utf-8", errors="replace").strip()
            except Exception:
                text = ""
            payload = parse_json_safely(text)

        response_content = format_http_error(response.status_code, payload, text)
        if on_token:
            on_token(response_content)

        response_time_ms = int((time.time() - start_time) * 1000)
        if tokens_used == 0 and response_content:
            tokens_used = estimate_tokens(response_content)
        msg = Message(
            role="assistant",
            content=response_content,
            thinking=None,
            tokens=tokens_used,
            response_time_ms=response_time_ms,
        )
        return _finalize_message_metadata(
            msg,
            detected_thinking_key=detected_thinking_key,
            thinking_present=False,
            show_thinking=show_thinking,
            runtime_error=runtime_error,
            http_status=http_status,
        )

    # Normal SSE stream
    tool_calls_buffer: List[dict] = []
    anthropic_tool_blocks: Dict[int, dict] = {}
    anthropic_block_index: Optional[int] = None
    responses_tool_calls_by_item: Dict[str, dict] = {}
    responses_current_output_index: Optional[int] = None
    responses_current_item_id = ""
    responses_reasoning_items: Dict[str, dict[str, Any]] = {}
    anthropic_reasoning_blocks: Dict[int, dict[str, Any]] = {}
    ollama_calls: dict[str, dict[str, Any]] = {}
    chat_reasoning_details: dict[int, dict[str, Any]] = {}
    terminal_received = False

    async for data in iter_sse_data_lines(response, cancel_event=cancel_event, log_fp=log_fp):
        if data == "[DONE]":
            terminal_received = True
            break
        try:
            chunk_data = parse_sse_json(data)
        except json.JSONDecodeError:
            if log_fp:
                try:
                    log_fp.write("[JSONDecodeError]\n")
                    log_fp.flush()
                except Exception as exc:
                    logger.debug("Failed to write JSON decode marker to stream log: %s", exc)
            continue

        observed_metadata: dict[str, Any] = {}
        if isinstance(chunk_data, dict):
            observed_metadata = _response_metadata_from_payload(
                chunk_data,
                response_format=response_format,
            )
            _merge_response_metadata(
                response_metadata,
                observed_metadata,
            )
            finish_reason = str(observed_metadata.get("finish_reason") or "")
            terminal_received = terminal_received or bool(
                (finish_reason and finish_reason not in {"in_progress", "queued"})
                or chunk_data.get("type") in {"message_stop", "response.completed"}
                or (response_format == "ollama_chat" and chunk_data.get("done") is True)
            )

            # Stop at the provider's terminal failure event.  In particular,
            # compatible gateways may report ``status=failed`` without an
            # ``error`` envelope; preserving this chunk is the only way to
            # retain its diagnostic details and to avoid buffering partial
            # tool calls after the failure.
            if observed_metadata.get("runtime_error"):
                runtime_error = True
                failure_payload = chunk_data
                failure_text = _format_provider_failure(
                    chunk_data,
                    source=("responses stream" if response_format in {"responses", "openai_responses"} else "stream"),
                )
                response_content = (
                    f"{response_content.rstrip()}\n\n{failure_text}"
                    if response_content.strip()
                    else failure_text
                )
                if on_token:
                    on_token(failure_text)
                break

            if observed_metadata.get("incomplete") and str(chunk_data.get("type") or "").strip().lower() in {
                "response.incomplete",
                "response.cancelled",
                "response.canceled",
            }:
                # The terminal Responses event may contain no useful delta;
                # keep the partial text/metadata collected so far and stop.
                break

        if isinstance(chunk_data, dict) and chunk_data.get("error") is not None:
            runtime_error = True
            failure_payload = chunk_data
            response_content = _format_provider_failure(chunk_data, source="stream")
            if on_token:
                on_token(response_content)
            break

        if response_format == "ollama_chat" and isinstance(chunk_data, dict):
            content, thinking, calls, parsed_tokens = parse_ollama_message(chunk_data)
            if content:
                visible, embedded_thinking = thinking_parser.feed(content)
                if visible:
                    response_content += visible
                    if on_token:
                        on_token(visible)
                if embedded_thinking:
                    thinking_present = True
                    thinking_content += embedded_thinking
                    if show_thinking and on_thinking:
                        on_thinking(embedded_thinking)
            if thinking:
                detected_thinking_key = "thinking"
                thinking_present = True
                thinking_content += thinking
                if show_thinking and on_thinking:
                    on_thinking(thinking)
            for call in calls:
                ollama_calls[str(call.get("id") or len(ollama_calls))] = call
            if parsed_tokens:
                tokens_used = parsed_tokens
            continue

        if response_format in {"responses", "openai_responses"} and isinstance(chunk_data, dict) and chunk_data.get("type"):
            event_type = str(chunk_data.get("type") or "")

            if event_type == "response.output_text.delta":
                content = str(chunk_data.get("delta") or "")
                if content:
                    visible, embedded_thinking = thinking_parser.feed(content)
                    if visible:
                        response_content += visible
                        if on_token:
                            on_token(visible)
                    if embedded_thinking:
                        thinking_present = True
                        thinking_content += embedded_thinking
                        if show_thinking and on_thinking:
                            on_thinking(embedded_thinking)
                continue

            if event_type in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
                thinking = str(chunk_data.get("delta") or "")
                detected_thinking_key = "reasoning"
                thinking_present = True
                if thinking:
                    thinking_content += thinking
                    if show_thinking and on_thinking:
                        on_thinking(thinking)
                continue

            if event_type == "response.output_item.added":
                item = chunk_data.get("item") if isinstance(chunk_data.get("item"), dict) else {}
                responses_current_output_index = chunk_data.get("output_index") if chunk_data.get("output_index") is not None else responses_current_output_index
                responses_current_item_id = str(item.get("id") or responses_current_item_id or "")
                if item.get("type") == "reasoning":
                    key = _responses_tool_call_key(
                        chunk_data,
                        responses_current_output_index,
                        responses_current_item_id,
                        len(responses_reasoning_items),
                    )
                    responses_reasoning_items[key] = copy.deepcopy(item)
                if item.get("type") == "function_call":
                    key = _responses_tool_call_key(
                        chunk_data,
                        responses_current_output_index,
                        responses_current_item_id,
                        len(responses_tool_calls_by_item),
                    )
                    _sync_responses_function_call_item(responses_tool_calls_by_item, key, item)
                continue

            if event_type in {"response.function_call_arguments.delta", "response.output_item.delta"}:
                delta = chunk_data.get("delta")
                if isinstance(delta, dict):
                    partial = str(delta.get("arguments") or delta.get("partial_json") or "")
                    name = str(delta.get("name") or "")
                else:
                    partial = str(delta or "")
                    name = ""
                key = _responses_tool_call_key(
                    chunk_data,
                    responses_current_output_index,
                    responses_current_item_id,
                    len(responses_tool_calls_by_item),
                )
                tcb = _ensure_responses_tool_call(
                    responses_tool_calls_by_item,
                    key,
                    call_id=str(chunk_data.get("call_id") or ""),
                    name=name,
                )
                if partial:
                    tcb["function"]["arguments"] += partial
                continue

            if event_type == "response.function_call_arguments.done":
                key = _responses_tool_call_key(
                    chunk_data,
                    responses_current_output_index,
                    responses_current_item_id,
                    len(responses_tool_calls_by_item),
                )
                tcb = _ensure_responses_tool_call(
                    responses_tool_calls_by_item,
                    key,
                    call_id=str(chunk_data.get("call_id") or ""),
                    name=str(chunk_data.get("name") or ""),
                )
                if chunk_data.get("arguments") is not None:
                    tcb["function"]["arguments"] = _responses_arguments_to_string(chunk_data.get("arguments"))
                continue

            if event_type == "response.output_item.done":
                item = chunk_data.get("item") if isinstance(chunk_data.get("item"), dict) else {}
                responses_current_output_index = chunk_data.get("output_index") if chunk_data.get("output_index") is not None else responses_current_output_index
                responses_current_item_id = str(item.get("id") or responses_current_item_id or "")
                key = _responses_tool_call_key(
                    chunk_data,
                    responses_current_output_index,
                    responses_current_item_id,
                    len(responses_tool_calls_by_item),
                )
                if item.get("type") == "reasoning":
                    responses_reasoning_items[key] = copy.deepcopy(item)
                _sync_responses_function_call_item(responses_tool_calls_by_item, key, item)
                continue

            if event_type == "response.completed":
                response_payload = chunk_data.get("response") if isinstance(chunk_data.get("response"), dict) else {}
                if response_payload:
                    content, thinking, tool_calls, parsed_tokens, detected = _parse_responses_payload(response_payload)
                    native_reasoning = _responses_reasoning_items(response_payload)
                    if native_reasoning:
                        reasoning_state = {
                            "codec": reasoning_codec or "responses_effort",
                            "items": native_reasoning,
                        }
                    detected_thinking_key = detected
                    if not response_content and content:
                        visible, embedded_thinking = thinking_parser.feed(content)
                        response_content += visible
                        if on_token and visible:
                            on_token(visible)
                        if embedded_thinking:
                            thinking_present = True
                            thinking_content += embedded_thinking
                    if thinking:
                        thinking_present = True
                    if thinking and thinking not in thinking_content:
                        thinking_content += thinking
                    if tool_calls:
                        response_tool_calls = tool_calls
                    if parsed_tokens:
                        tokens_used = parsed_tokens
                continue

            continue

        if isinstance(chunk_data, dict) and chunk_data.get("type"):
            event_type = str(chunk_data.get("type") or "")
            if event_type == "content_block_start":
                index = int(chunk_data.get("index") or 0)
                anthropic_block_index = index
                block = chunk_data.get("content_block") if isinstance(chunk_data.get("content_block"), dict) else {}
                block_type = str(block.get("type") or "")
                if block_type in {"thinking", "redacted_thinking"}:
                    anthropic_reasoning_blocks[index] = copy.deepcopy(block)
                    initial_thinking = str(block.get("thinking") or "") if block_type == "thinking" else ""
                    if initial_thinking:
                        detected_thinking_key = "thinking"
                        thinking_present = True
                        thinking_content += initial_thinking
                        if show_thinking and on_thinking:
                            on_thinking(initial_thinking)
                elif block_type == "tool_use":
                    anthropic_tool_blocks[index] = {
                        "id": str(block.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(block.get("name") or ""),
                            "arguments": "",
                        },
                    }
                continue

            if event_type == "content_block_delta":
                index = int(chunk_data.get("index") if chunk_data.get("index") is not None else (anthropic_block_index or 0))
                delta = chunk_data.get("delta") if isinstance(chunk_data.get("delta"), dict) else {}
                delta_type = str(delta.get("type") or "")
                if delta_type == "text_delta":
                    content = str(delta.get("text") or "")
                    if content:
                        visible, embedded_thinking = thinking_parser.feed(content)
                        if visible:
                            response_content += visible
                            if on_token:
                                on_token(visible)
                        if embedded_thinking:
                            thinking_present = True
                            thinking_content += embedded_thinking
                            if show_thinking and on_thinking:
                                on_thinking(embedded_thinking)
                elif delta_type == "thinking_delta":
                    thinking = str(delta.get("thinking") or "")
                    block = anthropic_reasoning_blocks.setdefault(index, {"type": "thinking", "thinking": ""})
                    block["thinking"] = str(block.get("thinking") or "") + thinking
                    detected_thinking_key = "thinking"
                    thinking_present = True
                    if thinking:
                        thinking_content += thinking
                        if show_thinking and on_thinking:
                            on_thinking(thinking)
                elif delta_type == "signature_delta":
                    signature = str(delta.get("signature") or "")
                    block = anthropic_reasoning_blocks.setdefault(index, {"type": "thinking", "thinking": ""})
                    block["signature"] = str(block.get("signature") or "") + signature
                elif delta_type == "input_json_delta":
                    partial = str(delta.get("partial_json") or "")
                    if partial:
                        tcb = anthropic_tool_blocks.setdefault(
                            index,
                            {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            },
                        )
                        tcb["function"]["arguments"] += partial
                continue

            if event_type == "message_delta":
                usage = chunk_data.get("usage") if isinstance(chunk_data.get("usage"), dict) else {}
                try:
                    tokens_used += int(usage.get("output_tokens") or 0)
                except Exception:
                    pass
                continue

        choices = chunk_data.get("choices", []) if isinstance(chunk_data, dict) else []
        if choices:
            delta = choices[0].get("delta", {}) or {}

            if normalize_reasoning_codec(reasoning_codec) == CHAT_REASONING_CODEC:
                _merge_chat_reasoning_details(chat_reasoning_details, delta.get("reasoning_details"))

            # Accumulate tool calls
            chunk_tool_calls = delta.get("tool_calls")
            if chunk_tool_calls:
                for tc in chunk_tool_calls:
                    index = tc.get("index", 0)
                    while len(tool_calls_buffer) <= index:
                        tool_calls_buffer.append({
                            "id": "", "type": "function",
                            "function": {"name": "", "arguments": ""},
                        })
                    tcb = tool_calls_buffer[index]
                    if tc.get("id"):
                        tcb["id"] = tc["id"]
                    if tc.get("type"):
                        tcb["type"] = tc["type"]
                    func = tc.get("function", {})
                    if func.get("name"):
                        tcb["function"]["name"] += func["name"]
                    if func.get("arguments"):
                        tcb["function"]["arguments"] += func["arguments"]

            # Content tokens
            content = delta.get("content", "") or ""
            if content:
                visible, embedded_thinking = thinking_parser.feed(content)
                if visible:
                    response_content += visible
                    if on_token:
                        on_token(visible)
                if embedded_thinking:
                    thinking_present = True
                    thinking_content += embedded_thinking
                    if show_thinking and on_thinking:
                        on_thinking(embedded_thinking)

            # Thinking fields
            thinking = ""
            for key in THINKING_KEYS:
                if key in delta and delta.get(key) is not None:
                    thinking, detected_thinking_key, thinking_present = _mark_reasoning_seen(
                        key=key,
                        value=delta.get(key),
                        detected_thinking_key=detected_thinking_key,
                    )
                    break
            if thinking:
                thinking_content += thinking
                if show_thinking and on_thinking:
                    on_thinking(thinking)

    if cancel_event is not None and cancel_event.is_set():
        raise asyncio.CancelledError
    if not terminal_received and not runtime_error:
        # EOF alone is not a completed model response. Discard this attempt's
        # partial tool calls and let the request pipeline retry the same body.
        raise httpx.RemoteProtocolError("Model stream closed before a terminal event")

    # Stream finished
    if log_fp:
        try:
            log_fp.write("\n===== END STREAM =====\n")
            log_fp.close()
        except Exception as exc:
            logger.debug("Failed to finalize stream log file: %s", exc)

    # Finalize tool calls
    if anthropic_tool_blocks:
        response_tool_calls = [
            tcb for _, tcb in sorted(anthropic_tool_blocks.items())
            if tcb.get("function", {}).get("name")
        ]
    if responses_tool_calls_by_item and response_tool_calls is None:
        response_tool_calls = [
            tcb for _, tcb in sorted(responses_tool_calls_by_item.items())
            if tcb.get("function", {}).get("name")
        ]
    if ollama_calls and response_tool_calls is None:
        response_tool_calls = list(ollama_calls.values())
    if tool_calls_buffer:
        response_tool_calls = [
            tcb for tcb in tool_calls_buffer
            if tcb.get("function", {}).get("name")
        ]
    if reasoning_state is None and responses_reasoning_items:
        reasoning_state = {
            "codec": reasoning_codec or "responses_effort",
            "items": [item for _, item in sorted(responses_reasoning_items.items())],
        }
    if anthropic_reasoning_blocks:
        reasoning_state = {
            "codec": reasoning_codec or "anthropic_adaptive",
            "items": [item for _, item in sorted(anthropic_reasoning_blocks.items())],
        }
    if normalize_reasoning_codec(reasoning_codec) == CHAT_REASONING_CODEC and chat_reasoning_details:
        reasoning_state = {
            "codec": CHAT_REASONING_CODEC,
            "items": [item for _, item in sorted(chat_reasoning_details.items())],
        }

    runtime_error = runtime_error or bool(response_metadata.get("runtime_error"))
    if runtime_error and not response_content.strip():
        response_content = _format_provider_failure(
            failure_payload or response_metadata,
            source="stream",
        )
        if on_token:
            on_token(response_content)

    response_time_ms = int((time.time() - start_time) * 1000)
    _complete_response_metadata(response_metadata)
    observed_total = _metadata_int(response_metadata.get("total_tokens"))
    if observed_total is not None:
        tokens_used = observed_total
    elif tokens_used == 0:
        tokens_used = estimate_tokens(response_content)

    msg = Message(
        role="assistant",
        content=response_content,
        thinking=thinking_content if thinking_content else None,
        tool_calls=response_tool_calls if response_tool_calls else None,
        tokens=tokens_used,
        response_time_ms=response_time_ms,
    )
    return _finalize_message_metadata(
        msg,
        detected_thinking_key=detected_thinking_key,
        thinking_present=thinking_present,
        show_thinking=show_thinking,
        runtime_error=runtime_error,
        http_status=http_status,
        reasoning_state=reasoning_state,
        response_metadata=response_metadata,
    )
