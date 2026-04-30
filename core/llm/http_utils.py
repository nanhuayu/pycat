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
    """Iterate `data: ...` lines from an OpenAI-compatible SSE stream."""

    buffer = ""
    decoder = codecs.getincrementaldecoder("utf-8")(errors='replace')

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
            line = line.strip()
            if not line:
                continue

            if not line.startswith("data: "):
                continue

            data = line[6:]
            if data == "[DONE]":
                continue

            if log_fp:
                try:
                    log_fp.write(data + "\n")
                    log_fp.flush()
                except Exception as exc:
                    logger.debug("Failed to write SSE debug chunk: %s", exc)

            yield data


def parse_sse_json(data: str) -> Any:
    return json.loads(data)
