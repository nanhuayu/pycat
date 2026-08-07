"""LLM execution module - handles LLM API calls with retry logic.

Owns request-scoped policy overrides, the pre-send context gate, provider
payload construction, streaming, and retry.
"""
from __future__ import annotations

import copy
import logging
import threading
from datetime import datetime
from typing import Any, Callable, Optional

from models.conversation import Conversation, Message
from models.provider import Provider

from core.llm.client import LLMClient
from core.context.maintainer import ContextMaintainer
from core.config import AppConfig
from core.llm.token_budget import (
    TokenBudget,
    estimate_conversation_tokens,
    estimate_request_tokens,
    resolve_token_budget,
)
from core.context.builder import prepare_api_messages, prepare_context_messages
from core.content.session_content import (
    RequestContentCache,
    SessionContentService,
    text_attachment_byte_budget,
)
from core.prompts.renderer import PromptRenderer
from core.prompts.channel import build_channel_prompt_section
from core.prompts.sections import PromptSections
from core.prompts.project_instructions import ProjectInstructionService
from core.context.providers.memory import selected_memory_sources
from core.skills import build_skill_prompt_section
from core.memory.service import MemoryService
from models.contracts.agent import RunPolicy, RunEventKind
from models.contracts.tooling import ToolAvailabilityContext, ToolSelectionPolicy
from core.tools.manager import ToolManager
from core.agent.request.retry import classify_error, retry_with_backoff
from core.observability.debug_trace import DebugTraceContext, ensure_debug_trace

logger = logging.getLogger(__name__)


class RequestPipeline:
    """Handles LLM API calls with retry and streaming support."""

    def __init__(
        self,
        client: LLMClient,
        *,
        tool_manager: ToolManager,
        context_maintenance: ContextMaintainer,
        prompt_renderer: PromptRenderer,
        content_service: SessionContentService | None = None,
        app_config: AppConfig | None = None,
    ):
        self._client = client
        self._tool_manager = tool_manager
        self._context_maintenance = context_maintenance
        self._prompt_renderer = prompt_renderer
        self._content_service = content_service
        self._app_config = app_config or AppConfig()

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
        debug_trace: DebugTraceContext | None = None,
        debug_turn: int = 0,
        debug_purpose: str = "main",
        memory_advice: str = "",
        stable_prompt_sections: PromptSections | None = None,
        emit: Optional[Callable[..., None]] = None,
        compression_tasks: Any = None,
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

        trace_context = ensure_debug_trace(debug_trace)
        attempt_count = 0
        request_conversation, request_body = await self._prepare_request(
            provider=provider,
            conversation=conversation,
            policy=policy,
            runtime_messages=runtime_messages,
            memory_advice=memory_advice,
            stable_prompt_sections=stable_prompt_sections,
            debug_trace=trace_context,
            content_cache=RequestContentCache(),
            compression_tasks=compression_tasks,
        )

        async def attempt() -> Message:
            nonlocal attempt_count
            attempt_count += 1
            purpose = debug_purpose if attempt_count <= 1 else "retry"
            return await self._send_prepared_request(
                provider=provider,
                request_conversation=request_conversation,
                request_body=request_body,
                policy=policy,
                on_token=on_token,
                on_thinking=on_thinking,
                cancel_event=cancel_event,
                debug_log_path=debug_log_path,
                debug_trace=trace_context,
                debug_turn=debug_turn,
                debug_purpose=purpose,
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
                    kind=RunEventKind.RETRY,
                    detail=f"Retry {attempt_num} after {delay:.1f}s ({kind.name})",
                )
            if trace_context is not None:
                trace_context.record_event(
                    kind="retry",
                    phase="event",
                    turn=int(debug_turn or trace_context.turn or 0),
                    node_id=f"retry{attempt_num:02d}",
                    parent_id=trace_context.parent_id,
                    name=kind.name.lower(),
                    status="scheduled",
                    summary=f"Retry {attempt_num} after {delay:.1f}s ({kind.name})",
                    data={"attempt": attempt_num, "delay_seconds": delay, "error": str(exc)},
                )

        return await retry_with_backoff(
            attempt,
            policy=policy.retry,
            on_retry=on_retry_fn,
        )

    async def _prepare_request(
        self,
        *,
        provider: Provider,
        conversation: Conversation,
        policy: RunPolicy,
        runtime_messages: Optional[list[Message]] = None,
        memory_advice: str = "",
        stable_prompt_sections: PromptSections | None = None,
        debug_trace: DebugTraceContext | None = None,
        content_cache: RequestContentCache | None = None,
        compression_tasks: Any = None,
    ) -> tuple[Conversation, dict[str, Any]]:
        """Build one immutable logical request before transport retries."""
        request_conversation = self._build_request_conversation(
            conversation=conversation,
            provider=provider,
            policy=policy,
        )
        token_budget = self._resolve_prompt_budget(
            conversation=request_conversation,
            provider=provider,
        )
        prepared_tools = await self._get_request_tools(
            conversation=request_conversation,
            policy=policy,
        )
        request_conversation, prepared_messages, replay_pressure = await self._apply_context_gate(
            conversation=conversation,
            request_conversation=request_conversation,
            provider=provider,
            policy=policy,
            tools=prepared_tools,
            runtime_messages=runtime_messages,
            memory_advice=memory_advice,
            stable_prompt_sections=stable_prompt_sections,
            debug_trace=debug_trace,
            content_cache=content_cache,
            compression_tasks=compression_tasks,
            token_budget=token_budget,
        )
        prepared_messages, tool_result_renderer = prepare_api_messages(
            prepared_messages,
            conversation=request_conversation,
            replay_pressure=replay_pressure,
        )
        api_messages = self._prompt_renderer.build_api_messages(
            prepared_messages,
            provider,
            conversation=request_conversation,
            tool_result_renderer=tool_result_renderer,
        )
        request_body = self._prompt_renderer.build_request_body(
            provider,
            request_conversation,
            api_messages,
            tools=prepared_tools,
            reasoning_mode=policy.reasoning_mode,
            token_budget=token_budget,
            pycat_assistant_enabled=policy.pycat_assistant_enabled,
        )
        return request_conversation, request_body

    async def _send_prepared_request(
        self,
        *,
        provider: Provider,
        request_conversation: Conversation,
        request_body: dict[str, Any],
        policy: RunPolicy,
        on_token: Optional[Callable[[str], None]] = None,
        on_thinking: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        debug_log_path: Optional[str] = None,
        debug_trace: DebugTraceContext | None = None,
        debug_turn: int = 0,
        debug_purpose: str = "main",
    ) -> Message:
        """Send an already prepared request without rebuilding prompt state."""
        return await self._client.send_request(
            provider=provider,
            request_body=request_body,
            on_token=on_token,
            on_thinking=on_thinking,
            show_thinking=bool(policy.show_thinking),
            debug_log_path=debug_log_path,
            cancel_event=cancel_event,
            debug_trace=debug_trace,
            debug_turn=debug_turn,
            debug_purpose=debug_purpose,
            conversation_id=str(getattr(request_conversation, "id", "") or ""),
            model_hint=str(getattr(request_conversation, "model", "") or ""),
        )

    async def _apply_context_gate(
        self,
        *,
        conversation: Conversation,
        request_conversation: Conversation,
        provider: Provider,
        policy: RunPolicy,
        tools: list[dict],
        runtime_messages: Optional[list[Message]],
        memory_advice: str = "",
        stable_prompt_sections: PromptSections | None = None,
        debug_trace: DebugTraceContext | None = None,
        content_cache: RequestContentCache | None = None,
        compression_tasks: Any = None,
        token_budget: TokenBudget | None = None,
    ) -> tuple[Conversation, list[Message], str]:
        """Apply request replay shaping, pre-send compaction, and the hard-window gate."""
        app_config = self._app_config
        compression_policy = getattr(getattr(app_config, "context", None), "compression_policy", None)
        tight_threshold_ratio = float(
            getattr(compression_policy, "tight_replay_threshold_ratio", 0.35) or 0.35
        )
        threshold_ratio = float(getattr(compression_policy, "token_threshold_ratio", 0.80) or 0.80)
        tight_threshold_ratio = max(0.10, min(0.94, tight_threshold_ratio))
        threshold_ratio = max(tight_threshold_ratio + 0.01, min(0.95, threshold_ratio))
        budget = token_budget or self._resolve_prompt_budget(
            conversation=conversation,
            provider=provider,
        )
        prompt_limit = max(1, int(budget.effective_prompt_limit or budget.context_window or 0))
        attachment_budget = text_attachment_byte_budget(prompt_limit)
        request_content_cache = content_cache or RequestContentCache()
        captured_at = datetime.now().astimezone()
        memory_prompt = self._build_memory_prompt(request_conversation)
        if memory_advice:
            memory_prompt = "\n\n".join(part for part in (memory_prompt, memory_advice) if part)
        tight_threshold_tokens = int(prompt_limit * tight_threshold_ratio)
        threshold_tokens = int(prompt_limit * threshold_ratio)
        current_messages: list[Message] | None = None
        conversation_estimate = self._estimate_active_history_tokens(
            conversation,
            text_byte_budget=attachment_budget,
            cache=request_content_cache,
        )
        compression_errors: list[str] = []

        async def prepare_messages() -> list[Message]:
            messages = await self._prepare_messages(
                conversation=request_conversation,
                provider=provider,
                policy=policy,
                tools=tools,
                debug_trace=debug_trace,
                memory_prompt=memory_prompt,
                captured_at=captured_at,
                stable_prompt_sections=stable_prompt_sections,
                token_budget=budget,
            )
            if runtime_messages:
                runtime_copy = copy.deepcopy(runtime_messages)
                if messages and messages[-1].metadata.get("context_kind") == "current_state":
                    current_state = messages.pop()
                    messages.extend(runtime_copy)
                    messages.append(current_state)
                else:
                    messages.extend(runtime_copy)
            if not any(getattr(message, "content_refs", None) for message in messages):
                return messages
            if self._content_service is None:
                raise RuntimeError("content service is required for session input references")
            return self._content_service.materialize_messages(
                request_conversation,
                messages,
                text_byte_budget=attachment_budget,
                cache=request_content_cache,
            )

        async def compact(*, reason: str) -> None:
            nonlocal captured_at, request_conversation, current_messages, conversation_estimate
            request_estimate = 0
            if current_messages is not None:
                request_estimate = self._estimate_request_tokens(
                    conversation=request_conversation,
                    provider=provider,
                    messages=current_messages,
                    tools=tools,
                    app_config=app_config,
                    pycat_assistant_enabled=policy.pycat_assistant_enabled,
                    token_budget=budget,
                )
            try:
                report = await self._context_maintenance.maintain_async(
                    conversation,
                    provider=provider,
                    policy=policy,
                    client=self._client,
                    prompt_limit=prompt_limit,
                    force=True,
                    honor_auto_enabled=False,
                    token_threshold_ratio=threshold_ratio,
                    request_token_estimate=request_estimate,
                    conversation_token_estimate=conversation_estimate,
                    debug_trace=debug_trace.with_purpose("condense") if debug_trace is not None else None,
                )
                logger.info("Pre-send compact (%s): %s", reason, report)
            except Exception as exc:
                compression_errors.append(str(exc))
                logger.warning("Pre-send compact failed (%s): %s", reason, exc)
                captured_at = datetime.now().astimezone()
                return
            request_conversation = self._build_request_conversation(
                conversation=conversation,
                provider=provider,
                policy=policy,
            )
            captured_at = datetime.now().astimezone()
            current_messages = await prepare_messages()
            conversation_estimate = self._estimate_active_history_tokens(
                conversation,
                text_byte_budget=attachment_budget,
                cache=request_content_cache,
            )

        if prompt_limit <= 1:
            # Degenerate budget (no resolvable model window): the gate cannot
            # measure anything meaningful, so send without compaction.
            return request_conversation, await prepare_messages(), "normal"

        if (
            conversation_estimate >= threshold_tokens
            and self._context_maintenance.auto_enabled(policy)
        ):
            await compact(reason="threshold")

        if current_messages is None:
            current_messages = await prepare_messages()

        async def choose_replay(messages: list[Message]) -> tuple[str | None, int, int]:
            normal_estimate = self._estimate_request_tokens(
                conversation=request_conversation,
                provider=provider,
                messages=messages,
                tools=tools,
                app_config=app_config,
                pycat_assistant_enabled=policy.pycat_assistant_enabled,
                token_budget=budget,
            )
            if normal_estimate < tight_threshold_tokens:
                if compression_tasks is not None:
                    compression_tasks.tight_pressure = False
                return "normal", normal_estimate, normal_estimate
            if compression_tasks is not None and not bool(getattr(compression_tasks, "tight_pressure", False)):
                await compression_tasks.drain()
                try:
                    compression_tasks.tight_pressure = True
                except Exception:
                    pass
                normal_estimate = self._estimate_request_tokens(
                    conversation=request_conversation,
                    provider=provider,
                    messages=messages,
                    tools=tools,
                    app_config=app_config,
                    pycat_assistant_enabled=policy.pycat_assistant_enabled,
                    token_budget=budget,
                )
                if normal_estimate < tight_threshold_tokens:
                    compression_tasks.tight_pressure = False
                    return "normal", normal_estimate, normal_estimate
            tight_estimate = self._estimate_request_tokens(
                conversation=request_conversation,
                provider=provider,
                messages=messages,
                tools=tools,
                app_config=app_config,
                replay_pressure="tight",
                pycat_assistant_enabled=policy.pycat_assistant_enabled,
                token_budget=budget,
            )
            if tight_estimate < normal_estimate and tight_estimate <= prompt_limit:
                logger.info(
                    "Tight tool replay selected: normal=%s tight=%s threshold=%s limit=%s",
                    normal_estimate,
                    tight_estimate,
                    tight_threshold_tokens,
                    prompt_limit,
                )
                return "tight", normal_estimate, tight_estimate
            if normal_estimate <= prompt_limit:
                return "normal", normal_estimate, tight_estimate
            return None, normal_estimate, tight_estimate

        replay_pressure, request_estimate, tight_estimate = await choose_replay(current_messages)
        if replay_pressure is not None:
            return request_conversation, current_messages, replay_pressure

        await compact(reason="hard_limit")
        replay_pressure, request_estimate, tight_estimate = await choose_replay(current_messages)
        if replay_pressure is not None:
            return request_conversation, current_messages, replay_pressure

        detail = f"；压缩失败：{compression_errors[-1]}" if compression_errors else ""
        raise RuntimeError(
            "上下文超过模型可用窗口，压缩和紧凑重放后仍无法发送："
            f"normal={request_estimate}, tight={tight_estimate}, limit={prompt_limit}{detail}"
        )

    def _estimate_active_history_tokens(
        self,
        conversation: Conversation,
        *,
        text_byte_budget: int | None = None,
        cache: RequestContentCache | None = None,
    ) -> int:
        messages = [
            message
            for message in (getattr(conversation, "messages", []) or [])
            if not getattr(message, "archived_content_id", None)
        ]
        if not any(getattr(message, "content_refs", None) for message in messages):
            return estimate_conversation_tokens(messages)
        if self._content_service is None:
            raise RuntimeError("content service is required for session input references")
        materialized = self._content_service.materialize_messages(
            conversation,
            messages,
            include_image_data=False,
            text_byte_budget=text_byte_budget,
            cache=cache,
        )
        return estimate_conversation_tokens(materialized)

    def _estimate_request_tokens(
        self,
        *,
        conversation: Conversation,
        provider: Provider,
        messages: list[Message],
        tools: list[dict],
        app_config: AppConfig,
        replay_pressure: str = "normal",
        pycat_assistant_enabled: bool = True,
        token_budget: TokenBudget | None = None,
    ) -> int:
        try:
            prepared_messages, tool_result_renderer = prepare_api_messages(
                messages,
                conversation=conversation,
                replay_pressure=replay_pressure,
            )
            api_messages = self._prompt_renderer.build_api_messages(
                prepared_messages,
                provider,
                conversation=conversation,
                tool_result_renderer=tool_result_renderer,
            )
            body = self._prompt_renderer.build_request_body(
                provider,
                conversation,
                api_messages,
                tools=tools,
                app_config=app_config,
                pycat_assistant_enabled=pycat_assistant_enabled,
                token_budget=token_budget,
            )
            return estimate_request_tokens(body)
        except Exception as exc:
            logger.debug("Failed to estimate request tokens, falling back to message text: %s", exc)
            text = "\n".join(str(getattr(msg, "content", "") or "") for msg in messages)
            return estimate_tokens(text)

    async def _prepare_messages(
        self,
        *,
        conversation: Conversation,
        provider: Provider,
        policy: RunPolicy,
        tools: list[dict],
        debug_trace: DebugTraceContext | None = None,
        memory_prompt: str | None = None,
        captured_at: datetime | None = None,
        stable_prompt_sections: PromptSections | None = None,
        token_budget: TokenBudget | None = None,
    ) -> list[Message]:
        """Prepare effective history + system prompt."""
        app_config = self._app_config

        sections = self._build_prompt_sections(
            conversation,
            app_config,
            tools,
            pycat_assistant_enabled=policy.pycat_assistant_enabled,
            stable_sections=stable_prompt_sections,
        )
        if memory_prompt is None:
            memory_prompt = self._build_memory_prompt(conversation)
        context_messages = prepare_context_messages(
            conversation=conversation,
            app_config=app_config,
            keep_last_turns=3,
            default_work_dir=getattr(conversation, "work_dir", ".") or ".",
            memory_prompt=memory_prompt,
            include_environment=policy.pycat_assistant_enabled,
            captured_at=captured_at,
            prompt_limit=int(
                getattr(token_budget, "effective_prompt_limit", 0)
                or getattr(token_budget, "context_window", 0)
                or 0
            ),
        )
        system_content = self._prompt_renderer.resolve_system_prompt(
            conversation,
            tools,
            provider,
            app_config=app_config,
            sections=sections,
            pycat_assistant_enabled=policy.pycat_assistant_enabled,
        )
        if system_content:
            return [Message(role="system", content=system_content)] + context_messages
        return context_messages

    @classmethod
    def _build_prompt_sections(
        cls,
        conversation: Conversation,
        app_config: AppConfig,
        tools: list[dict],
        *,
        pycat_assistant_enabled: bool = True,
        stable_sections: PromptSections | None = None,
    ) -> PromptSections:
        stable = stable_sections or cls.build_stable_prompt_sections(
            conversation,
            app_config,
            pycat_assistant_enabled=pycat_assistant_enabled,
        )
        return PromptSections(
            channel=stable.channel,
            project_instructions=stable.project_instructions,
            skills=cls._build_skill_prompt_section(
                conversation,
                tools,
                pycat_assistant_enabled=pycat_assistant_enabled,
            ),
        )

    @classmethod
    def build_stable_prompt_sections(
        cls,
        conversation: Conversation,
        app_config: AppConfig,
        *,
        pycat_assistant_enabled: bool = True,
    ) -> PromptSections:
        """Capture run-local provenance and project instructions."""
        return PromptSections(
            channel=cls._build_channel_prompt_section(conversation, app_config),
            project_instructions=(
                cls._build_project_instruction_section(conversation)
                if pycat_assistant_enabled
                else ""
            ),
        )

    @staticmethod
    def _build_channel_prompt_section(conversation: Conversation, app_config: AppConfig) -> str:
        settings = getattr(conversation, "settings", {}) or {}
        try:
            binding = settings.get("channel_binding") if isinstance(settings, dict) else None
            if not isinstance(binding, dict):
                return ""

            binding_source = str(binding.get("source") or "").strip()
            binding_channel_id = str(binding.get("channel_id") or "").strip()
            if not binding_channel_id or not binding_source:
                return ""

            matched_channel = None
            for channel in getattr(app_config, "channels", []) or []:
                if not bool(getattr(channel, "enabled", False)):
                    continue
                channel_id = str(getattr(channel, "id", "") or "").strip()
                source = str(getattr(channel, "source", "") or "").strip()
                if source != binding_source:
                    continue
                if channel_id != binding_channel_id:
                    continue
                matched_channel = channel
                break
            if matched_channel is None:
                return ""

            def _normalize(values) -> tuple[str, ...]:
                if isinstance(values, str):
                    candidates = [part.strip() for part in values.split(",")]
                elif isinstance(values, (list, tuple, set)):
                    candidates = [str(item).strip() for item in values]
                else:
                    candidates = []
                result: list[str] = []
                for item in candidates:
                    if item and item not in result:
                        result.append(item)
                return tuple(result)

            configured = (binding_source,)
            allowed_values = _normalize(settings.get("allowed_channel_sources"))
            allowed = tuple(source for source in (allowed_values or configured) if source == binding_source)
            trusted = tuple(item for item in _normalize(settings.get("trusted_channel_sources")) if item in allowed)
            notice_policy = str(settings.get("channel_notice_policy", "notice") or "notice").strip().lower() or "notice"

            messages = []
            for message in getattr(conversation, "messages", []) or []:
                metadata = getattr(message, "metadata", {}) or {}
                channel_meta = metadata.get("channel") if isinstance(metadata, dict) else None
                if not isinstance(channel_meta, dict):
                    continue
                source = str(channel_meta.get("source") or channel_meta.get("channel_source") or "").strip()
                if source == binding_source:
                    messages.append(message)
            return build_channel_prompt_section(
                messages,
                configured_sources=configured,
                allowed_sources=allowed,
                trusted_sources=trusted,
                notice_policy=notice_policy,
            )
        except Exception as exc:
            logger.debug("Failed to build channel prompt section: %s", exc)
            return ""

    @staticmethod
    def _build_memory_prompt(conversation: Conversation) -> str:
        try:
            return MemoryService.build_prompt_section(
                conversation.get_state(),
                RequestPipeline._latest_user_query(conversation),
                work_dir=getattr(conversation, "work_dir", ".") or ".",
                sources=selected_memory_sources(conversation),
            )
        except Exception as exc:
            logger.debug("Failed to build memory prompt section: %s", exc)
            return ""

    @staticmethod
    def _build_project_instruction_section(conversation: Conversation) -> str:
        try:
            return ProjectInstructionService.build_prompt_section(
                getattr(conversation, "work_dir", ".") or ".",
            )
        except Exception as exc:
            logger.debug("Failed to build project instruction section: %s", exc)
            return ""

    @staticmethod
    def _build_skill_prompt_section(
        conversation: Conversation,
        tools: list[dict],
        *,
        pycat_assistant_enabled: bool = True,
    ) -> str:
        try:
            return build_skill_prompt_section(
                conversation,
                tools,
                pycat_assistant_enabled=pycat_assistant_enabled,
            )
        except Exception as exc:
            logger.debug("Failed to build skill prompt section: %s", exc)
            return ""

    @staticmethod
    def _latest_user_query(conversation: Conversation) -> str:
        for msg in reversed(getattr(conversation, "messages", []) or []):
            if getattr(msg, "role", "") != "user":
                continue
            content = str(getattr(msg, "content", "") or "").strip()
            if content:
                return content
        return ""

    @staticmethod
    def _resolve_prompt_budget(*, conversation: Conversation, provider: Provider):
        return resolve_token_budget(
            conversation,
            provider=provider,
        )

    async def _get_request_tools(
        self,
        *,
        conversation: Conversation,
        policy: RunPolicy,
    ) -> list[dict]:
        selection = getattr(policy, "tool_selection", None) or ToolSelectionPolicy.all()
        try:
            return await self._tool_manager.get_all_tools(
                tool_selection=selection,
                availability_context=ToolAvailabilityContext(
                    work_dir=str(getattr(conversation, "work_dir", "") or ""),
                    conversation_id=str(getattr(conversation, "id", "") or ""),
                    source=str(getattr(policy, "source", "") or "desktop"),
                    search_available=self._tool_manager.search_service.is_available(),
                    mcp_available=bool(getattr(__import__("core.tools.manager", fromlist=["MCP_AVAILABLE"]), "MCP_AVAILABLE", False)),
                    completion_policy=str(getattr(policy, "completion_policy", "text") or "text"),
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
            try:
                setattr(request_conversation, "_source_conversation", conversation)
            except Exception:
                pass
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
            )
        return request_conversation
