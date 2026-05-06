"""Response parsing for LLM API calls.

Extracts non-streaming and streaming response handling from
``LLMClient.send_message`` so that ``client.py`` stays focused
on orchestration.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

import httpx

from models.conversation import Message
from core.llm.thinking_parser import ThinkingStreamParser
from core.llm.http_utils import (
    pretty_json,
    read_response_bytes,
    format_http_error,
    parse_json_safely,
    iter_sse_data_lines,
    parse_sse_json,
)
from core.llm.token_utils import estimate_tokens

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


def _finalize_message_metadata(
    msg: Message,
    *,
    detected_thinking_key: str,
    thinking_present: bool,
    enable_thinking: bool,
    runtime_error: bool = False,
    http_status: int | None = None,
) -> Message:
    msg.metadata["thinking_key"] = detected_thinking_key
    if thinking_present:
        msg.metadata["thinking_present"] = True
        if not enable_thinking:
            msg.metadata["thinking_hidden"] = True
    if runtime_error:
        msg.metadata["runtime_error"] = True
        if http_status is not None:
            msg.metadata["http_status"] = http_status
    return msg


def _json_dumps_compact(value: Any) -> str:
    try:
        return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return "{}"


def _parse_anthropic_content_blocks(payload: Dict[str, Any]) -> tuple[str, List[Dict[str, Any]], int]:
    """Return visible text, OpenAI-style tool calls, and token usage from Anthropic Messages JSON."""
    text_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []

    for block in payload.get("content", []) or []:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "").strip()
        if block_type == "text":
            text = str(block.get("text") or "")
            if text:
                text_parts.append(text)
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

    return "".join(text_parts), tool_calls, tokens


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
    enable_thinking: bool,
    response_format: str = "chat",
    on_token: Optional[Callable[[str], None]],
    start_time: float,
) -> Message:
    """Parse a non-streaming (``stream=false``) HTTP response into a ``Message``."""
    response_content = ""
    thinking_content = ""
    tokens_used = 0
    response_tool_calls: Optional[List[Dict[str, Any]]] = None
    detected_thinking_key = "reasoning_content"
    thinking_present = False
    runtime_error = False
    http_status: int | None = None

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
        if response_format == "responses" and isinstance(payload, dict):
            content, thinking, tool_calls, tokens_used, detected_thinking_key = _parse_responses_payload(payload)
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
        elif isinstance(payload, dict) and isinstance(payload.get("content"), list):
            content, tool_calls, tokens_used = _parse_anthropic_content_blocks(payload)
            visible, embedded_thinking = thinking_parser.feed(content)
            response_content += visible
            if embedded_thinking:
                thinking_present = True
                thinking_content += embedded_thinking
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

    if on_token and response_content:
        on_token(response_content)

    response_time_ms = int((time.time() - start_time) * 1000)
    if tokens_used == 0 and response_content:
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
        enable_thinking=enable_thinking,
        runtime_error=runtime_error,
        http_status=http_status,
    )


async def parse_stream_response(
    response: httpx.Response,
    *,
    thinking_parser: ThinkingStreamParser,
    enable_thinking: bool,
    response_format: str = "chat",
    on_token: Optional[Callable[[str], None]],
    on_thinking: Optional[Callable[[str], None]],
    cancel_event,
    log_fp,
    start_time: float,
) -> Message:
    """Consume an SSE stream and return the final ``Message``."""
    response_content = ""
    thinking_content = ""
    tokens_used = 0
    response_tool_calls: Optional[List[Dict[str, Any]]] = None
    detected_thinking_key = "reasoning_content"
    thinking_present = False
    runtime_error = False
    http_status: int | None = None

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
            enable_thinking=enable_thinking,
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

    async for data in iter_sse_data_lines(response, cancel_event=cancel_event, log_fp=log_fp):
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

        if isinstance(chunk_data, dict) and chunk_data.get("error") is not None:
            response_content = "接口返回错误（stream）：\n" + pretty_json(chunk_data.get("error"))
            if on_token:
                on_token(response_content)
            break

        if response_format == "responses" and isinstance(chunk_data, dict) and chunk_data.get("type"):
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
                        if enable_thinking and on_thinking:
                            on_thinking(embedded_thinking)
                continue

            if event_type in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
                thinking = str(chunk_data.get("delta") or "")
                detected_thinking_key = "reasoning"
                thinking_present = True
                if thinking:
                    thinking_content += thinking
                    if enable_thinking and on_thinking:
                        on_thinking(thinking)
                continue

            if event_type == "response.output_item.added":
                item = chunk_data.get("item") if isinstance(chunk_data.get("item"), dict) else {}
                responses_current_output_index = chunk_data.get("output_index") if chunk_data.get("output_index") is not None else responses_current_output_index
                responses_current_item_id = str(item.get("id") or responses_current_item_id or "")
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
                _sync_responses_function_call_item(responses_tool_calls_by_item, key, item)
                continue

            if event_type == "response.completed":
                response_payload = chunk_data.get("response") if isinstance(chunk_data.get("response"), dict) else {}
                if response_payload:
                    content, thinking, tool_calls, parsed_tokens, detected = _parse_responses_payload(response_payload)
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

            if event_type == "response.failed":
                runtime_error = True
                response_payload = chunk_data.get("response") if isinstance(chunk_data.get("response"), dict) else {}
                error = response_payload.get("error") if isinstance(response_payload.get("error"), dict) else chunk_data.get("error")
                response_content = "接口返回错误（responses stream）：\n" + pretty_json(error or response_payload or chunk_data)
                if on_token:
                    on_token(response_content)
                break

            continue

        if isinstance(chunk_data, dict) and chunk_data.get("type"):
            event_type = str(chunk_data.get("type") or "")
            if event_type == "content_block_start":
                index = int(chunk_data.get("index") or 0)
                anthropic_block_index = index
                block = chunk_data.get("content_block") if isinstance(chunk_data.get("content_block"), dict) else {}
                if block.get("type") == "tool_use":
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
                            if enable_thinking and on_thinking:
                                on_thinking(embedded_thinking)
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
                    if enable_thinking and on_thinking:
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
                if enable_thinking and on_thinking:
                    on_thinking(thinking)

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
    if tool_calls_buffer:
        response_tool_calls = [
            tcb for tcb in tool_calls_buffer
            if tcb.get("function", {}).get("name")
        ]

    response_time_ms = int((time.time() - start_time) * 1000)
    if tokens_used == 0:
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
        enable_thinking=enable_thinking,
        runtime_error=runtime_error,
        http_status=http_status,
    )
