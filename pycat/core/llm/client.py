"""LLM Client — HTTP transport and response parsing.

Delegates response parsing to ``pycat.core.llm.response_handler``.
"""

from __future__ import annotations

import logging
import time
import asyncio
from datetime import datetime
from typing import Any, Optional, Callable
import threading

import httpx

from pycat.models.provider import Provider
from pycat.models.conversation import Message

from pycat.core.llm.thinking_parser import ThinkingStreamParser
from pycat.core.llm.reasoning import normalize_reasoning_codec
from pycat.core.llm.images import request_image
from pycat.models.contracts.capability import ImageGenerationOptions
from pycat.core.llm.response_handler import parse_non_stream_response, parse_stream_response
from pycat.core.observability.debug_trace import DebugTraceContext, ensure_debug_trace


logger = logging.getLogger(__name__)


def _format_runtime_error(error: Exception) -> str:
    error_type = type(error).__name__
    detail = (str(error) or "").strip()
    if isinstance(error, httpx.ConnectError):
        # ConnectError often carries an empty message; give users an
        # actionable hint instead of a bare "未知错误".
        return (
            f"[{error_type}] 无法连接到服务端点，请检查网络、代理或服务商地址是否可用"
            if not detail
            else f"[{error_type}] 无法连接到服务端点（{detail}），请检查网络、代理或服务商地址"
        )
    if detail:
        return f"[{error_type}] {detail}"
    return f"[{error_type}] 未知错误"


class LLMClient:
    """Handles LLM transport, request body sending, and response parsing."""

    def __init__(self, timeout: float | None = None, *, transport_factory=None, headers_resolver=None):
        self._transport_factory = transport_factory
        self._headers_resolver = headers_resolver
        self.timeout = float(timeout if timeout is not None else 600.0)
        if self.timeout <= 0:
            self.timeout = 600.0

    def set_timeout(self, timeout: float) -> None:
        try:
            self.timeout = max(30.0, min(3600.0, float(timeout)))
        except Exception:
            logger.debug("Ignored invalid LLM timeout update: %r", timeout)

    async def generate_image(self, *, provider: Provider, model: str, prompt: str,
                             options: ImageGenerationOptions, images: list[bytes] | None = None,
                             mask: bytes | None = None, cancel_event=None) -> Message:
        return await request_image(provider=provider, model=model, prompt=prompt, options=options,
                                   images=images, mask=mask, cancel_event=cancel_event, timeout=self.timeout,
                                   transport_factory=self._transport_factory, headers_resolver=self._headers_resolver)

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
            if profile.model_type == "image":
                raise ValueError("生图模型请通过图像能力调用，不能作为聊天模型。")
            reasoning_codec = normalize_reasoning_codec(getattr(profile, "reasoning_codec", "none"))
            if self._headers_resolver is not None:
                headers = await self._headers_resolver(provider, logical_model)
            elif provider.auth_type in {'chatgpt', 'workbuddy'}:
                raise RuntimeError(f'当前宿主未配置 {provider.account_label} 登录服务。')
            else:
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
            combined_log_fp = trace_log_fp or log_fp

            timeout_config = httpx.Timeout(self.timeout, connect=60.0)
            transport = self._transport_factory() if self._transport_factory is not None else None
            async with httpx.AsyncClient(timeout=timeout_config, transport=transport) as client:
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
        metadata = getattr(msg, "metadata", {}) or {}
        if bool(metadata.get("incomplete")):
            status = "interrupted"
        elif bool(metadata.get("runtime_error")):
            status = "error"
        else:
            status = "completed"
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
        metrics = {
            "tokens": int(getattr(msg, "tokens", 0) or 0),
            "tool_calls": len(tool_calls),
            "response_time_ms": int(getattr(msg, "response_time_ms", 0) or 0),
        }
        for key in (
            "finish_reason",
            "prompt_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "total_tokens",
            "incomplete_reason",
        ):
            if metadata.get(key) is not None:
                metrics[key] = metadata[key]
        metrics["incomplete"] = bool(metadata.get("incomplete", False))
        debug_trace.sink.finish_llm(
            debug_trace,
            status=status,
            duration_ms=duration_ms,
            refs=refs,
            summary=str(getattr(msg, "summary", "") or getattr(msg, "content", "") or "")[:220],
            data=metrics,
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
