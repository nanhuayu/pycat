"""HTTP/SSE utilities for LLM API interactions.

Consolidates:
- JSON formatting and parsing
- HTTP error formatting
- SSE (Server-Sent Events) stream parsing
"""

from __future__ import annotations

import json
import codecs
import logging
from typing import Any, AsyncIterator, Optional, TextIO

import httpx


logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# JSON / Error formatting
# -----------------------------------------------------------------------------

def pretty_json(value: Any, max_chars: int = 12000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        text = str(value)

    if len(text) > max_chars:
        return text[:max_chars] + "\n...（内容过长，已截断）"
    return text


def parse_json_safely(text: str) -> Optional[Any]:
    try:
        return json.loads(text) if text else None
    except Exception:
        return None


def format_http_error(status_code: int, payload: Any, text_fallback: str = "") -> str:
    if isinstance(payload, dict) and payload.get("error") is not None:
        return f"HTTP 错误 {status_code}\n" + pretty_json(payload.get("error"))

    if payload is not None:
        return f"HTTP 错误 {status_code}\n" + pretty_json(payload)

    if text_fallback:
        return f"HTTP 错误 {status_code}: {text_fallback[:1200]}"

    return f"HTTP 错误 {status_code}"


# -----------------------------------------------------------------------------
# HTTP response helpers
# -----------------------------------------------------------------------------

async def read_response_bytes(resp: httpx.Response) -> bytes:
    try:
        return await resp.aread()
    except Exception:
        return b""


# -----------------------------------------------------------------------------
# SSE (Server-Sent Events) streaming
# -----------------------------------------------------------------------------

async def iter_sse_data_lines(
    response: httpx.Response,
    *,
    cancel_event: Optional[object] = None,
    log_fp: Optional[TextIO] = None,
) -> AsyncIterator[str]:
    """Iterate JSON payloads from an SSE stream.

    Supports both OpenAI Chat Completions style frames (``data: {...}``) and
    Responses API frames (``event: ...`` + ``data: {...}``). If an event name is
    present and the JSON payload has no ``type`` field, the event name is added
    as ``type`` so downstream parsers can use one code path.
    """

    buffer = ""
    event_lines: list[str] = []
    data_lines: list[str] = []
    decoder = codecs.getincrementaldecoder("utf-8")(errors='replace')

    def _emit_event_payload() -> str | None:
        nonlocal event_lines, data_lines
        if not data_lines:
            event_lines = []
            data_lines = []
            return None
        data = "\n".join(data_lines).strip()
        event_type = "\n".join(event_lines).strip()
        event_lines = []
        data_lines = []
        if not data or data == "[DONE]":
            return None
        if event_type and data.startswith("{"):
            payload = parse_json_safely(data)
            if isinstance(payload, dict) and "type" not in payload:
                payload["type"] = event_type
                return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return data

    def _write_log(payload: str) -> None:
        if not log_fp:
            return
        try:
            log_fp.write(payload + "\n")
            log_fp.flush()
        except Exception as exc:
            logger.debug("Failed to write SSE debug chunk: %s", exc)

    async for chunk in response.aiter_bytes():
        try:
            if cancel_event is not None and hasattr(cancel_event, "is_set") and cancel_event.is_set():
                return
        except Exception:
            return

        try:
            text_chunk = decoder.decode(chunk, final=False)
            buffer += text_chunk
        except Exception:
            continue

        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")
            if not line.strip():
                payload = _emit_event_payload()
                if payload:
                    _write_log(payload)
                    yield payload
                continue

            if line.startswith("event:"):
                event_lines.append(line.split(":", 1)[1].strip())
                continue

            if not line.startswith("data:"):
                continue

            data = line.split(":", 1)[1].lstrip()
            data_lines.append(data)

            if not event_lines:
                payload = _emit_event_payload()
                if payload:
                    _write_log(payload)
                    yield payload

    payload = _emit_event_payload()
    if payload:
        _write_log(payload)
        yield payload


def parse_sse_json(data: str) -> Any:
    return json.loads(data)
