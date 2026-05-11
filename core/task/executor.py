"""LLM execution module - handles LLM API calls with retry logic.

Extracted from task.py to reduce complexity and improve testability.
Responsibilities:
- Adapt RunPolicy overrides to the active conversation request
- Execute LLM calls with streaming support
- Handle retry logic with exponential backoff
"""
from __future__ import annotations

import copy
import logging
import threading
from typing import Callable, Optional

from models.conversation import Conversation, Message
from models.provider import Provider

from core.llm.client import LLMClient
from core.context.condenser import CondensePolicy, ContextCondenser
from core.context.manager import ContextManager
from core.config import AppConfig, load_app_config
from core.task.types import RunPolicy, TaskEventKind
from core.tools.catalog import ToolAvailabilityContext, ToolSelectionPolicy
from core.task.retry import classify_error, retry_with_backoff

logger = logging.getLogger(__name__)


class LLMExecutor:
    """Handles LLM API calls with retry and streaming support."""

    def __init__(self, client: LLMClient):
        self._client = client

    async def call_with_retry(
        self,
        *,
        provider: Provider,
        conversation: Conversation,
        policy: RunPolicy,
        runtime_messages: Optional[list[Message]] = None,
        on_token: Optional[Callable[[str], None]] = None,
        on_thinking: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        debug_log_path: Optional[str] = None,
        emit: Optional[Callable[..., None]] = None,
    ) -> Message:
        """Call LLM with retry logic on transient errors.

        Args:
            provider: LLM provider configuration
            conversation: Current conversation
            policy: Execution policy
            on_token: Callback for streaming tokens
            on_thinking: Callback for thinking content
            cancel_event: Event to signal cancellation
            debug_log_path: Path to save debug logs
            emit: Event emission callback

        Returns:
            Assistant message with response

        Raises:
            Exception: On non-retryable errors or after max retries
        """

        async def attempt() -> Message:
            return await self._call_raw(
                provider=provider,
                conversation=conversation,
                policy=policy,
                runtime_messages=runtime_messages,
                on_token=on_token,
                on_thinking=on_thinking,
                cancel_event=cancel_event,
                debug_log_path=debug_log_path,
            )

        def on_retry_fn(attempt_num: int, delay: float, exc: str | Exception) -> None:
            kind = classify_error(exc)
            logger.warning(
                "LLM call failed (attempt %d): %s - retrying in %.1fs",
                attempt_num,
                kind.name,
                delay,
            )
            if emit:
                emit(
                    kind=TaskEventKind.RETRY,
                    detail=f"Retry {attempt_num} after {delay:.1f}s ({kind.name})",
                )

        return await retry_with_backoff(
            attempt,
            policy=policy.retry,
            on_retry=on_retry_fn,
        )

    async def _call_raw(
        self,
        *,
        provider: Provider,
        conversation: Conversation,
        policy: RunPolicy,
        runtime_messages: Optional[list[Message]] = None,
        on_token: Optional[Callable[[str], None]] = None,
        on_thinking: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        debug_log_path: Optional[str] = None,
    ) -> Message:
        """Execute a single LLM API call without retry.

        Args:
            provider: LLM provider configuration
            conversation: Current conversation
            policy: Execution policy
            on_token: Callback for streaming tokens
            on_thinking: Callback for thinking content
            cancel_event: Event to signal cancellation
            debug_log_path: Path to save debug logs

        Returns:
            Assistant message with response
        """
        request_conversation = self._build_request_conversation(
            conversation=conversation,
            provider=provider,
            policy=policy,
        )
        prepared_tools = await self._get_request_tools(
            conversation=request_conversation,
            policy=policy,
        )
        prepared_messages = await self._prepare_messages(
            conversation=request_conversation,
            provider=provider,
            policy=policy,
            tools=prepared_tools,
        )
        if runtime_messages:
            prepared_messages.extend(copy.deepcopy(runtime_messages))

        return await self._client.send_message(
            provider=provider,
            conversation=request_conversation,
            on_token=on_token,
            on_thinking=on_thinking,
            enable_thinking=bool(policy.enable_thinking),
            debug_log_path=debug_log_path,
            cancel_event=cancel_event,
            prepared_messages=prepared_messages,
            prepared_tools=prepared_tools,
        )

    async def _prepare_messages(
        self,
        *,
        conversation: Conversation,
        provider: Provider,
        policy: RunPolicy,
        tools: list[dict],
    ) -> list[Message]:
        """Prepare effective history + system prompt through ContextManager."""
        try:
            app_config = load_app_config()
        except Exception as e:
            logger.debug("Failed to load app config for message preparation: %s", e)
            app_config = AppConfig()

        context_manager = ContextManager(
            condenser=ContextCondenser(self._client),
            policy=CondensePolicy(
                max_active_messages=20,
                token_threshold_ratio=0.7,
                keep_last_n=3,
            ),
        )
        return await context_manager.prepare_messages(
            conversation=conversation,
            provider=provider,
            context_window_limit=int(policy.context_window_limit),
            tools=tools,
            app_config=app_config,
            default_work_dir=getattr(conversation, "work_dir", ".") or ".",
            compress=False,
        )

    async def _get_request_tools(
        self,
        *,
        conversation: Conversation,
        policy: RunPolicy,
    ) -> list[dict]:
        prepared_query = ""
        try:
            for msg in reversed(getattr(conversation, "messages", []) or []):
                if getattr(msg, "role", "") != "user":
                    continue
                prepared_query = (getattr(msg, "content", "") or "").strip()
                if prepared_query:
                    break
        except Exception as e:
            logger.debug("Failed to derive prepared query for tool loading: %s", e)
            prepared_query = ""

        base_selection = getattr(policy, "tool_selection", None) or ToolSelectionPolicy.all()
        selection = base_selection
        if prepared_query:
            selection = selection.with_prepared_queries([prepared_query])

        try:
            return await self._client.tool_manager.get_all_tools(
                tool_selection=selection,
                availability_context=ToolAvailabilityContext(
                    work_dir=str(getattr(conversation, "work_dir", ".") or "."),
                    conversation_id=str(getattr(conversation, "id", "") or ""),
                    search_available=self._client.tool_manager.search_service.is_available(),
                    mcp_available=bool(getattr(__import__("core.tools.manager", fromlist=["MCP_AVAILABLE"]), "MCP_AVAILABLE", False)),
                ),
                tool_permissions=policy.tool_permissions,
            )
        except Exception as e:
            logger.warning("Failed to load request tools: %s", e)
            return []

    def _build_request_conversation(
        self,
        *,
        conversation: Conversation,
        provider: Provider,
        policy: RunPolicy,
    ) -> Conversation:
        """Create a request-scoped conversation with RunPolicy overrides applied."""
        try:
            request_conversation = Conversation.from_dict(conversation.to_dict())
        except Exception as e:
            logger.warning("Failed to clone conversation for LLM request: %s", e)
            request_conversation = conversation

        request_conversation.mode = str(getattr(policy, "mode", "") or request_conversation.mode or "chat")

        try:
            llm_config = request_conversation.get_llm_config()
            updates: dict[str, object] = {}
            if policy.temperature is not None:
                updates["temperature"] = float(policy.temperature)
            if policy.max_tokens is not None:
                updates["max_tokens"] = int(policy.max_tokens)
            if policy.model is not None:
                updates["model"] = str(policy.model).strip()
            if not llm_config.model and getattr(provider, "default_model", None):
                updates.setdefault("model", str(getattr(provider, "default_model", "") or "").strip())
            if updates:
                llm_config = llm_config.with_updates(**updates)
            request_conversation.set_llm_config(llm_config)
        except Exception as e:
            logger.warning("Failed to apply llm_config overrides to request conversation: %s", e)
            request_settings = dict(request_conversation.settings or {})
            if policy.temperature is not None:
                request_settings["temperature"] = float(policy.temperature)
            if policy.max_tokens is not None:
                request_settings["max_tokens"] = int(policy.max_tokens)
            request_conversation.settings = request_settings
            request_conversation.model = (
                str(policy.model or "").strip()
                or str(getattr(request_conversation, "model", "") or "").strip()
                or str(getattr(provider, "default_model", "") or "").strip()
            )
        return request_conversation
