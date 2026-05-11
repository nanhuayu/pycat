"""LLM Client — orchestrates request building, HTTP transport, and response parsing.

Delegates response parsing to ``core.llm.response_handler``.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any, Optional, Callable
import threading

import httpx

from models.provider import Provider
from models.conversation import Message, Conversation

from core.llm.thinking_parser import ThinkingStreamParser
from core.config import load_app_config, AppConfig
from core.llm.response_handler import parse_non_stream_response, parse_stream_response
from core.prompts.assembler import PromptAssembler


logger = logging.getLogger(__name__)


def _format_runtime_error(error: Exception) -> str:
    error_type = type(error).__name__
    detail = (str(error) or "").strip()
    if detail:
        return f"[{error_type}] {detail}"
    return f"[{error_type}] 未知错误"


class LLMClient:
    """Handles chat interactions with LLM providers."""

    def __init__(self, timeout: float | None = None, tool_manager=None, prompt_assembler: PromptAssembler | None = None):
        if timeout is None:
            try:
                timeout = float(load_app_config().llm_timeout_seconds)
            except Exception as exc:
                logger.debug("Failed to load timeout from app config, using default: %s", exc)
                timeout = 600.0
        self.timeout = float(timeout)
        if tool_manager is not None:
            self.tool_manager = tool_manager
        else:
            from core.tools.manager import ToolManager

            self.tool_manager = ToolManager()
        self.prompt_assembler = prompt_assembler or PromptAssembler()

    def set_timeout(self, timeout: float) -> None:
        try:
            self.timeout = max(30.0, min(3600.0, float(timeout)))
        except Exception:
            logger.debug("Ignored invalid LLM timeout update: %r", timeout)

    async def send_message(
        self,
        provider: Provider,
        conversation: Conversation,
        on_token: Optional[Callable[[str], None]] = None,
        on_thinking: Optional[Callable[[str], None]] = None,
        enable_thinking: bool = True,
        debug_log_path: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
        prepared_messages: Optional[list[Message]] = None,
        prepared_tools: Optional[list[dict]] = None,
    ) -> Message:
        start_time = time.time()

        thinking_parser = ThinkingStreamParser()
        log_fp = None
        if debug_log_path:
            try:
                log_fp = open(debug_log_path, "a", encoding="utf-8")
                log_fp.write(f"\n===== {datetime.now().isoformat(timespec='seconds')} START =====\n")
                log_fp.flush()
            except Exception as exc:
                logger.debug("Failed to open debug log file %s: %s", debug_log_path, exc)
                log_fp = None

        try:
            try:
                app_config = load_app_config()
            except Exception:
                app_config = AppConfig()

            tools = prepared_tools or []

            if prepared_messages is not None:
                base_messages = prepared_messages
            else:
                base_messages = self.prompt_assembler.select_base_messages(conversation, app_config=app_config)
            api_messages = self.prompt_assembler.build_api_messages(
                base_messages,
                provider,
                conversation=conversation,
            )
            request_body = self.prompt_assembler.build_request_body(
                provider,
                conversation,
                api_messages,
                tools=tools,
                app_config=app_config,
            )
            response_format = "responses" if getattr(provider, "is_openai_responses", False) else "chat"

            if debug_log_path:
                try:
                    open(f"debug_request_{int(time.time())}.json", "w", encoding="utf-8").write(
                        json.dumps(request_body, ensure_ascii=False, indent=2)
                    )
                except Exception as exc:
                    logger.debug("Failed to write debug request snapshot: %s", exc)

            timeout_config = httpx.Timeout(self.timeout, connect=60.0)
            async with httpx.AsyncClient(timeout=timeout_config) as client:
                # ===== Non-stream mode =====
                if not request_body.get("stream", True):
                    resp = await client.post(
                        provider.get_chat_endpoint(),
                        headers=provider.get_headers(),
                        json=request_body,
                    )
                    msg = parse_non_stream_response(
                        resp,
                        thinking_parser=thinking_parser,
                        enable_thinking=enable_thinking,
                        response_format=response_format,
                        on_token=on_token,
                        start_time=start_time,
                    )
                    self._attach_metadata(msg, provider, request_body, conversation)
                    return msg

                # ===== Streaming mode =====
                async with client.stream(
                    "POST",
                    provider.get_chat_endpoint(),
                    headers=provider.get_headers(),
                    json=request_body,
                ) as response:
                    msg = await parse_stream_response(
                        response,
                        thinking_parser=thinking_parser,
                        enable_thinking=enable_thinking,
                        response_format=response_format,
                        on_token=on_token,
                        on_thinking=on_thinking,
                        cancel_event=cancel_event,
                        log_fp=log_fp,
                        start_time=start_time,
                    )
                    self._attach_metadata(msg, provider, request_body, conversation)
                    return msg

        except Exception as e:
            logger.exception("LLM send_message failed: %s", _format_runtime_error(e))
            raise RuntimeError(f"Error sending message: {_format_runtime_error(e)}") from e

    @staticmethod
    def _attach_metadata(
        msg: Message,
        provider: Provider,
        request_body: dict,
        conversation: Conversation,
    ) -> None:
        """Attach provider / model metadata to the response message."""
        try:
            msg.metadata.update({
                "provider_id": getattr(provider, "id", ""),
                "provider_name": getattr(provider, "name", ""),
                "model": request_body.get("model")
                if isinstance(request_body, dict)
                else (conversation.model or provider.default_model),
                "thinking_key": msg.metadata.get("thinking_key", "reasoning_content"),
            })
        except Exception as exc:
            logger.debug("Failed to attach LLM response metadata: %s", exc)
