"""LLM Client — HTTP transport and response parsing.

Delegates response parsing to ``core.llm.response_handler``.
"""

from __future__ import annotations

import logging
import time
import asyncio
from datetime import datetime
from typing import Any, Optional, Callable
import threading

import httpx

from models.provider import Provider
from models.conversation import Message

from core.llm.thinking_parser import ThinkingStreamParser
from core.llm.reasoning import normalize_reasoning_codec
from core.llm.response_handler import parse_non_stream_response, parse_stream_response
from core.observability.debug_trace import DebugTraceContext, ensure_debug_trace


logger = logging.getLogger(__name__)


class _TeeLogFile:
    def __init__(self, *files):
        self._files = [fp for fp in files if fp is not None]

    def write(self, text: str) -> None:
        for fp in list(self._files):
            try:
                fp.write(text)
            except Exception as exc:
                logger.debug("Failed to write tee debug stream: %s", exc)

    def flush(self) -> None:
        for fp in list(self._files):
            try:
                fp.flush()
            except Exception as exc:
                logger.debug("Failed to flush tee debug stream: %s", exc)

    def close(self) -> None:
        for fp in list(self._files):
            try:
                fp.close()
            except Exception as exc:
                logger.debug("Failed to close tee debug stream: %s", exc)


def _format_runtime_error(error: Exception) -> str:
    error_type = type(error).__name__
    detail = (str(error) or "").strip()
    if detail:
        return f"[{error_type}] {detail}"
    return f"[{error_type}] 未知错误"


class LLMClient:
    """Handles LLM transport, request body sending, and response parsing."""

    def __init__(self, timeout: float | None = None):
        self.timeout = float(timeout if timeout is not None else 600.0)
        if self.timeout <= 0:
            self.timeout = 600.0

    def set_timeout(self, timeout: float) -> None:
        try:
            self.timeout = max(30.0, min(3600.0, float(timeout)))
        except Exception:
            logger.debug("Ignored invalid LLM timeout update: %r", timeout)

    async def send_request(
        self,
        *,
        provider: Provider,
        request_body: dict[str, Any],
        on_token: Optional[Callable[[str], None]] = None,
        on_thinking: Optional[Callable[[str], None]] = None,
        show_thinking: bool = True,
        debug_log_path: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
        debug_trace: DebugTraceContext | None = None,
        debug_turn: int = 0,
        debug_purpose: str = "main",
        conversation_id: str = "",
        model_hint: str = "",
    ) -> Message:
        start_time = time.time()

        thinking_parser = ThinkingStreamParser()
        log_fp = None
        trace_log_fp = None
        combined_log_fp = None
        trace_context = ensure_debug_trace(debug_trace)
        llm_trace: DebugTraceContext | None = None
        refs: dict[str, str] = {}
        use_legacy_stream_log = bool(debug_log_path) and not bool(
            trace_context is not None and getattr(trace_context.sink, "capture_stream", False)
        )
        if use_legacy_stream_log:
            try:
                log_fp = open(debug_log_path, "a", encoding="utf-8")
                log_fp.write(f"\n===== {datetime.now().isoformat(timespec='seconds')} START =====\n")
                log_fp.flush()
            except Exception as exc:
                logger.debug("Failed to open debug log file %s: %s", debug_log_path, exc)
                log_fp = None

        try:
            response_format = str(getattr(provider, "api_type", "") or "openai_compatible")
            logical_model = str(model_hint or request_body.get("model") or "").strip()
            profile = provider.effective_model_profile(logical_model)
            reasoning_codec = normalize_reasoning_codec(getattr(profile, "reasoning_codec", "none"))
            headers = provider.get_headers(logical_model)
            endpoint = provider.get_chat_endpoint()

            if trace_context is not None:
                purpose = debug_purpose or trace_context.default_purpose or "main"
                llm_trace = trace_context.sink.start_llm(
                    trace_context,
                    turn=int(debug_turn or trace_context.turn or 0),
                    purpose=purpose,
                    provider=str(getattr(provider, "name", "") or ""),
                    model=str(request_body.get("model") or model_hint or ""),
                    response_format=response_format,
                    include_stream=bool(request_body.get("stream", True)),
                )
                refs = dict(llm_trace.refs or {})
                if refs.get("request"):
                    trace_context.sink.write_json(
                        refs["request"],
                        {
                            "endpoint": endpoint,
                            "headers": headers,
                            "body": request_body,
                            "provider": {
                                "id": getattr(provider, "id", ""),
                                "name": getattr(provider, "name", ""),
                                "api_type": getattr(provider, "api_type", ""),
                            },
                            "conversation_id": str(conversation_id or ""),
                        },
                    )
                if refs.get("stream"):
                    trace_log_fp = trace_context.sink.open_stream_file(refs["stream"])
            if trace_log_fp is not None and log_fp is not None:
                combined_log_fp = _TeeLogFile(log_fp, trace_log_fp)
            else:
                combined_log_fp = trace_log_fp or log_fp

            timeout_config = httpx.Timeout(self.timeout, connect=60.0)
            async with httpx.AsyncClient(timeout=timeout_config) as client:
                # ===== Non-stream mode =====
                if not request_body.get("stream", True):
                    resp = await client.post(
                        endpoint,
                        headers=headers,
                        json=request_body,
                    )
                    msg = parse_non_stream_response(
                        resp,
                        thinking_parser=thinking_parser,
                        show_thinking=show_thinking,
                        response_format=response_format,
                        reasoning_codec=reasoning_codec,
                        on_token=on_token,
                        start_time=start_time,
                    )
                    self._attach_metadata(msg, provider, request_body, model_hint=model_hint)
                    self._record_debug_response(
                        llm_trace,
                        refs=refs,
                        msg=msg,
                        provider=provider,
                        response_format=response_format,
                        start_time=start_time,
                    )
                    return msg

                # ===== Streaming mode =====
                async with client.stream(
                    "POST",
                    endpoint,
                    headers=headers,
                    json=request_body,
                ) as response:
                    msg = await parse_stream_response(
                        response,
                        thinking_parser=thinking_parser,
                        show_thinking=show_thinking,
                        response_format=response_format,
                        reasoning_codec=reasoning_codec,
                        on_token=on_token,
                        on_thinking=on_thinking,
                        cancel_event=cancel_event,
                        log_fp=combined_log_fp,
                        start_time=start_time,
                    )
                    self._attach_metadata(msg, provider, request_body, model_hint=model_hint)
                    self._record_debug_response(
                        llm_trace,
                        refs=refs,
                        msg=msg,
                        provider=provider,
                        response_format=response_format,
                        start_time=start_time,
                    )
                    return msg

        except asyncio.CancelledError as e:
            self._record_debug_error(llm_trace, refs=refs, error=e, start_time=start_time)
            logger.debug("LLM send_message cancelled: %s", _format_runtime_error(e))
            raise
        except Exception as e:
            self._record_debug_error(llm_trace, refs=refs, error=e, start_time=start_time)
            logger.exception("LLM send_message failed: %s", _format_runtime_error(e))
            raise RuntimeError(f"Error sending message: {_format_runtime_error(e)}") from e
        finally:
            if combined_log_fp is not None:
                try:
                    combined_log_fp.close()
                except Exception as exc:
                    logger.debug("Failed to close combined debug log file: %s", exc)
            else:
                for fp in (trace_log_fp, log_fp):
                    if fp is not None:
                        try:
                            fp.close()
                        except Exception as exc:
                            logger.debug("Failed to close debug log file: %s", exc)

    @staticmethod
    def _attach_metadata(
        msg: Message,
        provider: Provider,
        request_body: dict,
        *,
        model_hint: str = "",
    ) -> None:
        """Attach provider / model metadata to the response message."""
        try:
            msg.metadata.update({
                "provider_id": getattr(provider, "id", ""),
                "provider_name": getattr(provider, "name", ""),
                "model": request_body.get("model")
                if isinstance(request_body, dict)
                else model_hint,
                "thinking_key": msg.metadata.get("thinking_key", "reasoning_content"),
            })
        except Exception as exc:
            logger.debug("Failed to attach LLM response metadata: %s", exc)

    @staticmethod
    def _record_debug_response(
        debug_trace: DebugTraceContext | None,
        *,
        refs: dict[str, str],
        msg: Message,
        provider: Provider,
        response_format: str,
        start_time: float,
    ) -> None:
        if debug_trace is None or not debug_trace.enabled:
            return
        status = "error" if bool((getattr(msg, "metadata", {}) or {}).get("runtime_error")) else "completed"
        duration_ms = int((time.time() - start_time) * 1000)
        if refs.get("response"):
            debug_trace.sink.write_json(
                refs["response"],
                {
                    "status": status,
                    "provider": {
                        "id": getattr(provider, "id", ""),
                        "name": getattr(provider, "name", ""),
                        "api_type": getattr(provider, "api_type", ""),
                    },
                    "response_format": response_format,
                    "duration_ms": duration_ms,
                    "message": msg.to_dict(),
                },
            )
        tool_calls = list(getattr(msg, "tool_calls", None) or [])
        debug_trace.sink.finish_llm(
            debug_trace,
            status=status,
            duration_ms=duration_ms,
            refs=refs,
            summary=str(getattr(msg, "summary", "") or getattr(msg, "content", "") or "")[:220],
            data={
                "tokens": int(getattr(msg, "tokens", 0) or 0),
                "tool_calls": len(tool_calls),
                "response_time_ms": int(getattr(msg, "response_time_ms", 0) or 0),
            },
        )

    @staticmethod
    def _record_debug_error(
        debug_trace: DebugTraceContext | None,
        *,
        refs: dict[str, str],
        error: Exception,
        start_time: float,
    ) -> None:
        if debug_trace is None or not debug_trace.enabled:
            return
        duration_ms = int((time.time() - start_time) * 1000)
        if refs.get("response"):
            debug_trace.sink.write_json(
                refs["response"],
                {
                    "status": "error",
                    "duration_ms": duration_ms,
                    "error": _format_runtime_error(error),
                    "error_type": type(error).__name__,
                },
            )
        debug_trace.sink.finish_llm(
            debug_trace,
            status="error",
            duration_ms=duration_ms,
            refs=refs,
            summary=_format_runtime_error(error)[:220],
            data={"error_type": type(error).__name__},
        )
