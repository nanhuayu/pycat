"""Unified task execution engine - main coordinator.

Simplified from 574 lines to ~200 lines by extracting:
- RequestPipeline: LLM API calls with retry
- ToolExecutor: Tool execution and state management
- EventEmitter: Event streaming

This module now focuses on orchestration:
- Turn-based execution loop
- Hook management
- Auto-continue logic
- Sub-task delegation
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Callable, Optional

from models.contracts.config import AppConfig
from models.conversation import Conversation, Message, normalize_tool_result
from models.provider import Provider

from core.llm.client import LLMClient
from core.prompts.renderer import PromptRenderer
from core.prompts.sections import PromptSections
from core.tools.manager import ToolManager
from core.tools.base import ToolResult
from core.context.maintainer import ContextMaintainer
from core.content.session_content import SessionContentService
from core.capabilities.compression import CapabilityCompressor
from models.contracts.agent import (
    RunPolicy,
    RunEvent,
    RunEventKind,
    RunResult,
    RunStopReason,
    RunStatus,
    TurnState,
    TurnContext,
    TurnOutcome,
    TurnOutcomeKind,
)
from core.agent.tooling.repetition import ToolRepetitionDetector
from core.agent.request.pipeline import RequestPipeline
from core.agent.tooling.executor import ToolExecutor
from core.agent.events.emitter import EventEmitter
from core.agent.events.conversation import (
    conversation_patch_payload,
    emit_conversation_patch,
)
from core.agent.delegation.subagent import build_root_run
from core.agent.run.stop_policy import TaskStopPolicy
from core.agent.run.control import RunControl, effective_run_policy
from core.agent.run.compression_tasks import CompressionTaskSet
from core.agent.delegation.runner import SubagentRunner
from core.agent.tooling.loop import ToolCallCoordinator
from core.agent.tooling.result_recorder import ToolResultRecorder
from core.observability.debug_trace import DebugTraceContext, ensure_debug_trace
from core.tools.base import ToolApprovalRequest
from core.agent.run.control_messages import EXPLICIT_COMPLETION_REMINDER, MAX_EXPLICIT_COMPLETION_RETRIES
from core.memory.advisor import MemoryAdvisor
from core.content.archive_store import SessionArchiveStore
from core.content.archive_view_service import ArchiveViewService
from core.tools.tool_call_archive import ToolResultViewService

logger = logging.getLogger(__name__)

class AgentRunEngine:
    """Unified think-act tool loop coordinator.

    Orchestrates LLM calls, tool execution, and event streaming.
    Delegates to specialized executors for each responsibility.
    """

    def __init__(
        self,
        *,
        client: LLMClient,
        tool_manager: ToolManager,
        prompt_renderer: PromptRenderer,
        capability_executor: Any = None,
        app_config: AppConfig | None = None,
        provider_catalog_provider: Callable[[], Iterable[Provider]] | None = None,
        context_maintenance: ContextMaintainer | None = None,
        content_service: SessionContentService | None = None,
    ) -> None:
        self._client = client
        self._tool_manager = tool_manager
        self._app_config = app_config or AppConfig()
        self._app_settings = self._app_config.to_dict()
        self._capability_executor = capability_executor
        compression_factory = self._build_compression_factory(capability_executor)
        self._compression_factory = compression_factory
        self._memory_advisor = MemoryAdvisor(capability_executor) if capability_executor is not None else None
        self._context_maintenance = context_maintenance or ContextMaintainer(
            client=client,
            app_config=self._app_config,
            compression_factory=compression_factory,
        )
        self._llm_executor = RequestPipeline(
            client,
            tool_manager=tool_manager,
            context_maintenance=self._context_maintenance,
            prompt_renderer=prompt_renderer,
            content_service=content_service,
            app_config=self._app_config,
        )
        self._tool_executor = ToolExecutor(
            tool_manager,
            capability_executor=capability_executor,
            compression_factory=compression_factory,
            shell_config=getattr(self._app_config, "shell", None),
            content_service=content_service,
        )
        self._subtask_coordinator = SubagentRunner(
            self,
            provider_catalog_provider=provider_catalog_provider,
        )
        self._tool_result_recorder = ToolResultRecorder(
            summarize_tool_result=self._summarize_tool_result,
        )
        self._pre_turn_hooks: list[Callable] = []
        self._post_turn_hooks: list[Callable] = []

    @staticmethod
    def _build_compression_factory(capability_executor: Any):
        if capability_executor is None:
            return None

        def factory(*, provider: Provider, debug_trace: Any = None):
            return CapabilityCompressor(
                provider,
                capability_executor=capability_executor,
                debug_trace=debug_trace,
            )

        return factory

    def add_pre_turn_hook(self, hook: Callable) -> None:
        """Register a hook called before each LLM turn. Signature: (conversation, turn, policy) -> None"""
        self._pre_turn_hooks.append(hook)

    def add_post_turn_hook(self, hook: Callable) -> None:
        """Register a hook called after each LLM turn. Signature: (conversation, turn, assistant_msg) -> None"""
        self._post_turn_hooks.append(hook)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(
        self,
        *,
        provider: Provider,
        conversation: Conversation,
        policy: RunPolicy,
        on_event: Optional[Callable[[RunEvent], None]] = None,
        on_token: Optional[Callable[[str], None]] = None,
        on_thinking: Optional[Callable[[str], None]] = None,
        approval_callback: Optional[Callable[[ToolApprovalRequest], Any]] = None,
        questions_callback: Optional[Callable[[dict[str, Any]], Any]] = None,
        cancel_event: Optional[threading.Event] = None,
        debug_log_path: Optional[str] = None,
        debug_trace: DebugTraceContext | None = None,
        initial_runtime_messages: Optional[list[Message]] = None,
        run_control: RunControl | None = None,
    ) -> RunResult:
        trace_context = ensure_debug_trace(debug_trace)
        compression_tasks = CompressionTaskSet(
            lambda content_id: self._get_or_create_archive_summary(
                content_id=content_id,
                provider=provider,
                conversation=conversation,
                debug_trace=trace_context,
            ),
            cancel_event=cancel_event,
        )
        try:
            return await self._run_with_compression_tasks(
                provider=provider,
                conversation=conversation,
                policy=policy,
                on_event=on_event,
                on_token=on_token,
                on_thinking=on_thinking,
                approval_callback=approval_callback,
                questions_callback=questions_callback,
                cancel_event=cancel_event,
                debug_log_path=debug_log_path,
                debug_trace=trace_context,
                initial_runtime_messages=initial_runtime_messages,
                run_control=run_control,
                compression_tasks=compression_tasks,
            )
        finally:
            await compression_tasks.cancel_all("agent run finished")

    async def _run_with_compression_tasks(
        self,
        *,
        provider: Provider,
        conversation: Conversation,
        policy: RunPolicy,
        on_event: Optional[Callable[[RunEvent], None]] = None,
        on_token: Optional[Callable[[str], None]] = None,
        on_thinking: Optional[Callable[[str], None]] = None,
        approval_callback: Optional[Callable[[ToolApprovalRequest], Any]] = None,
        questions_callback: Optional[Callable[[dict[str, Any]], Any]] = None,
        cancel_event: Optional[threading.Event] = None,
        debug_log_path: Optional[str] = None,
        debug_trace: DebugTraceContext | None = None,
        initial_runtime_messages: Optional[list[Message]] = None,
        run_control: RunControl | None = None,
        compression_tasks: CompressionTaskSet,
    ) -> RunResult:
        run = build_root_run(conversation=conversation, provider=provider, policy=policy)
        provider = run.provider
        conversation = run.conversation
        policy = run.policy
        turns_limit = max(1, int(policy.max_turns or 200))
        final_assistant: Optional[Message] = None
        repetition_detector = ToolRepetitionDetector()
        emitter = EventEmitter(on_event)
        trace_context = ensure_debug_trace(debug_trace)
        self._queue_missing_tool_summaries(conversation, compression_tasks)
        stable_prompt_sections: PromptSections = self._llm_executor.build_stable_prompt_sections(
            conversation,
            self._app_config,
            pycat_assistant_enabled=policy.pycat_assistant_enabled,
        )
        memory_advice = ""
        if not (cancel_event and cancel_event.is_set()):
            memory_advice = await self._advise_memory(
                provider=provider,
                conversation=conversation,
                policy=policy,
                debug_trace=trace_context,
            )
        turn_context = TurnContext(
            turn=0,
            runtime_messages=list(initial_runtime_messages or []),
            memory_advice=memory_advice,
        )

        for turn in range(turns_limit):
            turn_context.turn = turn + 1
            turn_trace = self._turn_trace_context(trace_context, turn_context.turn)
            turn_policy, _permission_revision = effective_run_policy(policy, run_control)
            outcome = await self._run_turn(
                provider=provider,
                conversation=conversation,
                policy=turn_policy,
                turn_context=turn_context,
                repetition_detector=repetition_detector,
                emitter=emitter,
                turns_limit=turns_limit,
                on_token=on_token,
                on_thinking=on_thinking,
                approval_callback=approval_callback,
                questions_callback=questions_callback,
                cancel_event=cancel_event,
                debug_log_path=debug_log_path,
                debug_trace=turn_trace,
                on_event=on_event,
                stable_prompt_sections=stable_prompt_sections,
                run_control=run_control,
                compression_tasks=compression_tasks,
            )
            self._record_turn_end(turn_trace, outcome)
            turn_context = outcome.context
            if outcome.final_message is not None:
                final_assistant = outcome.final_message
            guidance_terminal = outcome.kind == TurnOutcomeKind.COMPLETE or (
                outcome.kind == TurnOutcomeKind.INTERRUPTED
                and outcome.stop_reason == RunStopReason.EXPLICIT_COMPLETION_MISSING
            )
            if outcome.kind == TurnOutcomeKind.CONTINUE or guidance_terminal:
                guidance = self._take_runtime_guidance(
                    run_control,
                    terminal=guidance_terminal,
                    can_continue=turn + 1 < turns_limit,
                )
                if guidance:
                    if outcome.final_message is not None:
                        outcome.final_message.metadata["intermediate"] = True
                        outcome.final_message.metadata.pop("interrupted", None)
                        outcome.final_message.metadata.pop("interrupt_reason", None)
                    turn_context.runtime_messages = []
                    turn_context.incomplete_responses = 0
                    self._append_runtime_guidance(
                        conversation,
                        guidance,
                        emitter=emitter,
                        turn=turn_context.turn,
                    )
                    continue
            if outcome.kind == TurnOutcomeKind.CONTINUE:
                continue
            if outcome.kind == TurnOutcomeKind.CANCELLED:
                return RunResult(
                    status=RunStatus.CANCELLED,
                    final_message=final_assistant,
                    stop_reason=RunStopReason.CANCELLED,
                    conversation=conversation,
                )
            if outcome.kind == TurnOutcomeKind.INTERRUPTED:
                return RunResult(
                    status=RunStatus.INTERRUPTED,
                    final_message=outcome.final_message or final_assistant,
                    error=outcome.error,
                    stop_reason=outcome.stop_reason,
                    conversation=conversation,
                )
            if outcome.kind == TurnOutcomeKind.FAILED:
                return RunResult(
                    status=RunStatus.FAILED,
                    final_message=final_assistant,
                    error=outcome.error,
                    stop_reason=outcome.stop_reason or RunStopReason.ERROR,
                    conversation=conversation,
                )
            final_message = outcome.final_message or final_assistant
            state_version_before_curate = self._state_version(conversation)
            await self._curate_memory(
                provider=provider,
                conversation=conversation,
                policy=policy,
                final_message=final_message,
                debug_trace=turn_trace,
            )
            if self._state_version(conversation) > state_version_before_curate:
                self._emit_conversation_patch(
                    emitter,
                    conversation,
                    turn=turn_context.turn,
                    detail="Conversation state synchronized after memory review.",
                    include_messages=False,
                )
            return RunResult(
                status=RunStatus.COMPLETED,
                final_message=final_message,
                stop_reason=outcome.stop_reason or RunStopReason.COMPLETED,
                conversation=conversation,
            )

        fallback = self._build_max_turns_message(conversation=conversation, final_assistant=final_assistant)
        if fallback is not None:
            conversation.add_message(fallback)
            emitter.emit(RunEventKind.STEP, turn=turns_limit, data=fallback)
            emitter.emit(RunEventKind.COMPLETE, turn=turns_limit, data=fallback)
            return RunResult(
                status=RunStatus.INTERRUPTED,
                final_message=fallback,
                stop_reason=RunStopReason.MAX_TURNS,
                conversation=conversation,
            )
        return RunResult(
            status=RunStatus.INTERRUPTED,
            final_message=final_assistant,
            stop_reason=RunStopReason.MAX_TURNS,
            conversation=conversation,
        )

    async def _curate_memory(
        self,
        *,
        provider: Provider,
        conversation: Conversation,
        policy: RunPolicy,
        final_message: Message | None,
        debug_trace: DebugTraceContext | None = None,
    ) -> None:
        if self._memory_advisor is None:
            return
        settings = getattr(conversation, "settings", {}) or {}
        if str(getattr(policy, "source", "") or "").strip().lower() in {"sub_task", "capability"}:
            return
        if settings.get("parent_session_id"):
            return
        try:
            await self._memory_advisor.curate(
                provider=provider,
                conversation=conversation,
                final_message=final_message,
                debug_trace=debug_trace,
            )
        except Exception as exc:
            logger.debug("Run-end memory review failed: %s", exc)

    async def _advise_memory(
        self,
        *,
        provider: Provider,
        conversation: Conversation,
        policy: RunPolicy,
        debug_trace: DebugTraceContext | None = None,
    ) -> str:
        if self._memory_advisor is None:
            return ""
        settings = getattr(conversation, "settings", {}) or {}
        if str(getattr(policy, "source", "") or "").strip().lower() in {"sub_task", "capability"}:
            return ""
        if settings.get("parent_session_id"):
            return ""
        try:
            return await self._memory_advisor.advise_current(
                provider=provider,
                conversation=conversation,
                debug_trace=debug_trace,
            )
        except Exception as exc:
            logger.debug("Run-start memory advice failed: %s", exc)
            return ""

    @staticmethod
    def _trace_status_for_outcome(outcome: TurnOutcome) -> str:
        if outcome.kind == TurnOutcomeKind.FAILED:
            return "error"
        if outcome.kind == TurnOutcomeKind.CANCELLED:
            return "cancelled"
        if outcome.kind == TurnOutcomeKind.INTERRUPTED:
            return "interrupted"
        if outcome.kind == TurnOutcomeKind.CONTINUE:
            return "continue"
        return "completed"

    def _turn_trace_context(
        self,
        debug_trace: DebugTraceContext | None,
        turn: int,
    ) -> DebugTraceContext | None:
        if debug_trace is None:
            return None
        turn_number = max(0, int(turn or 0))
        scope_id = str(debug_trace.scope_id or "")
        node_id = debug_trace.sink.turn_node_id(turn_number, scope_id=scope_id)
        parent_id = str(debug_trace.node_id or debug_trace.parent_id or "run")
        return debug_trace.with_turn(turn_number).child(
            node_id=node_id,
            parent_id=parent_id,
            scope_id=scope_id,
            default_purpose="main",
        )

    def _record_turn_end(
        self,
        debug_trace: DebugTraceContext | None,
        outcome: TurnOutcome,
    ) -> None:
        if debug_trace is None:
            return
        final_message = outcome.final_message
        summary = ""
        if final_message is not None:
            summary = str(getattr(final_message, "summary", "") or getattr(final_message, "content", "") or "")[:220]
        if not summary:
            summary = str(outcome.error or outcome.kind.value or "")[:220]
        debug_trace.record_event(
            kind="turn",
            phase="end",
            turn=debug_trace.turn,
            node_id=debug_trace.node_id,
            parent_id=debug_trace.parent_id,
            name=f"turn{debug_trace.turn:02d}",
            status=self._trace_status_for_outcome(outcome),
            summary=summary,
            data={
                "outcome": outcome.kind.value,
                "stop_reason": getattr(outcome.stop_reason, "value", str(outcome.stop_reason)),
                "error": outcome.error or "",
            },
        )

    async def _run_turn(
        self,
        *,
        provider: Provider,
        conversation: Conversation,
        policy: RunPolicy,
        turn_context: TurnContext,
        repetition_detector: ToolRepetitionDetector,
        emitter: EventEmitter,
        turns_limit: int,
        on_token,
        on_thinking,
        approval_callback,
        questions_callback,
        cancel_event,
        debug_log_path,
        on_event,
        stable_prompt_sections: PromptSections,
        run_control: RunControl | None = None,
        debug_trace: DebugTraceContext | None = None,
        compression_tasks: CompressionTaskSet | None = None,
    ) -> TurnOutcome:
        if cancel_event and cancel_event.is_set():
            turn_context.state = TurnState.CANCELLED
            return TurnOutcome(kind=TurnOutcomeKind.CANCELLED, context=turn_context)

        emitter.emit(RunEventKind.TURN_START, turn=turn_context.turn, detail=f"Turn {turn_context.turn}/{turns_limit}")
        if debug_trace is not None:
            debug_trace.record_event(
                kind="turn",
                phase="start",
                turn=turn_context.turn,
                node_id=debug_trace.node_id,
                parent_id=debug_trace.parent_id,
                name=f"turn{turn_context.turn:02d}",
                status="running",
                summary=f"Turn {turn_context.turn}/{turns_limit}",
                data={"turns_limit": turns_limit},
            )
            debug_trace = debug_trace.child(default_purpose="main")
        state_version_before_request = self._state_version(conversation)
        turn_context.state = TurnState.PRE_TURN_HOOKS
        for hook in self._pre_turn_hooks:
            try:
                hook(conversation, turn_context.turn, policy)
            except Exception as he:
                logger.debug("Pre-turn hook error: %s", he)

        assistant_msg = await self._request_assistant_message(
            provider=provider,
            conversation=conversation,
            policy=policy,
            turn_context=turn_context,
            emitter=emitter,
            on_token=on_token,
            on_thinking=on_thinking,
            cancel_event=cancel_event,
            debug_log_path=debug_log_path,
            debug_trace=debug_trace,
            stable_prompt_sections=stable_prompt_sections,
            compression_tasks=compression_tasks,
        )
        if self._state_version(conversation) > state_version_before_request:
            self._emit_conversation_patch(
                emitter,
                conversation,
                turn=turn_context.turn,
                detail="Conversation state synchronized after context maintenance.",
            )
        if isinstance(assistant_msg, TurnOutcome):
            return assistant_msg

        turn_context.runtime_messages = []
        turn_context.state = TurnState.ASSISTANT_RECEIVED
        response_metadata = getattr(assistant_msg, "metadata", {}) or {}
        if bool(response_metadata.get("runtime_error")):
            return TurnOutcome(
                kind=TurnOutcomeKind.FAILED,
                context=turn_context,
                final_message=assistant_msg,
                error=str(getattr(assistant_msg, "content", "") or "模型接口返回错误。"),
                stop_reason=RunStopReason.ERROR,
            )

        response_incomplete = bool(response_metadata.get("incomplete"))
        if response_incomplete:
            raw_incomplete_reason = response_metadata.get("incomplete_reason")
            if not raw_incomplete_reason:
                finish_reason = str(response_metadata.get("finish_reason") or "").strip().lower()
                raw_incomplete_reason = (
                    "output_limit"
                    if finish_reason in {
                        "length",
                        "max_tokens",
                        "max_output_tokens",
                        "max_completion_tokens",
                        "output_limit",
                        "token_limit",
                    }
                    else "incomplete_response"
                )
            incomplete_reason = str(raw_incomplete_reason).strip().lower()
            output_limited = incomplete_reason == "output_limit"
            interrupt_reason = "output_limit" if output_limited else incomplete_reason
            assistant_msg.metadata.update(
                {
                    "completion": False,
                    "completion_policy": str(getattr(policy, "completion_policy", "text") or "text"),
                    "interrupted": True,
                    "interrupt_reason": interrupt_reason,
                }
            )
        conversation.add_message(assistant_msg)
        if response_incomplete:
            self._attach_state_snapshot(conversation, assistant_msg)
        emitter.emit(RunEventKind.STEP, turn=turn_context.turn, data=assistant_msg)

        if response_incomplete:
            turn_context.state = TurnState.TURN_COMPLETE
            return TurnOutcome(
                kind=TurnOutcomeKind.INTERRUPTED,
                context=turn_context,
                final_message=assistant_msg,
                error=(
                    "模型在生成完成前达到输出上限。"
                    if output_limited
                    else f"模型响应未完整结束（{interrupt_reason}）。"
                ),
                stop_reason=(
                    RunStopReason.OUTPUT_LIMIT
                    if output_limited
                    else RunStopReason.INCOMPLETE_RESPONSE
                ),
            )

        for hook in self._post_turn_hooks:
            try:
                hook(conversation, turn_context.turn, assistant_msg)
            except Exception as he:
                logger.debug("Post-turn hook error: %s", he)

        if cancel_event and cancel_event.is_set():
            turn_context.state = TurnState.CANCELLED
            return TurnOutcome(kind=TurnOutcomeKind.CANCELLED, context=turn_context, final_message=assistant_msg)

        if not assistant_msg.tool_calls:
            return await self._handle_turn_without_tools(
                provider=provider,
                conversation=conversation,
                policy=policy,
                turn_context=turn_context,
                assistant_msg=assistant_msg,
            )

        turn_context.state = TurnState.TOOL_EXECUTION
        return await self._execute_tool_calls(
            provider=provider,
            conversation=conversation,
            policy=policy,
            turn_context=turn_context,
            assistant_msg=assistant_msg,
            repetition_detector=repetition_detector,
            emitter=emitter,
            approval_callback=approval_callback,
            questions_callback=questions_callback,
            cancel_event=cancel_event,
            on_event=on_event,
            on_token=on_token,
            on_thinking=on_thinking,
            turns_limit=turns_limit,
            run_control=run_control,
            debug_trace=debug_trace,
            compression_tasks=compression_tasks,
        )

    async def _request_assistant_message(
        self,
        *,
        provider: Provider,
        conversation: Conversation,
        policy: RunPolicy,
        turn_context: TurnContext,
        emitter: EventEmitter,
        on_token,
        on_thinking,
        cancel_event,
        debug_log_path,
        debug_trace: DebugTraceContext | None,
        stable_prompt_sections: PromptSections,
        compression_tasks: CompressionTaskSet | None = None,
    ) -> Message | TurnOutcome:
        turn_context.state = TurnState.LLM_CALL
        try:
            return await self._llm_executor.call_with_retry(
                provider=provider,
                conversation=conversation,
                policy=policy,
                runtime_messages=turn_context.runtime_messages,
                on_token=on_token,
                on_thinking=on_thinking,
                cancel_event=cancel_event,
                debug_log_path=debug_log_path,
                debug_trace=debug_trace,
                debug_turn=turn_context.turn,
                debug_purpose="main",
                memory_advice=turn_context.memory_advice,
                stable_prompt_sections=stable_prompt_sections,
                emit=lambda **kw: emitter.emit(turn=turn_context.turn, **kw),
                compression_tasks=compression_tasks,
            )
        except Exception as e:
            turn_context.state = TurnState.FAILED
            return TurnOutcome(
                kind=TurnOutcomeKind.FAILED,
                context=turn_context,
                error=str(e),
                stop_reason=RunStopReason.ERROR,
            )

    async def _handle_turn_without_tools(
        self,
        *,
        provider: Provider,
        conversation: Conversation,
        policy: RunPolicy,
        turn_context: TurnContext,
        assistant_msg: Message,
    ) -> TurnOutcome:
        if str(getattr(policy, "completion_policy", "text") or "text") == "explicit":
            turn_context.incomplete_responses += 1
            assistant_msg.metadata["completion"] = False
            assistant_msg.metadata["completion_policy"] = "explicit"
            assistant_msg.metadata["intermediate"] = True
            self._attach_state_snapshot(conversation, assistant_msg)
            turn_context.state = TurnState.TURN_COMPLETE
            if turn_context.incomplete_responses <= MAX_EXPLICIT_COMPLETION_RETRIES:
                turn_context.runtime_messages = [Message(role="user", content=EXPLICIT_COMPLETION_REMINDER)]
                return TurnOutcome(
                    kind=TurnOutcomeKind.CONTINUE,
                    context=turn_context,
                    final_message=assistant_msg,
                )
            assistant_msg.metadata["interrupted"] = True
            assistant_msg.metadata["interrupt_reason"] = "explicit_completion_missing"
            return TurnOutcome(
                kind=TurnOutcomeKind.INTERRUPTED,
                context=turn_context,
                final_message=assistant_msg,
                stop_reason=RunStopReason.EXPLICIT_COMPLETION_MISSING,
            )

        self._attach_state_snapshot(conversation, assistant_msg)
        if not str(getattr(assistant_msg, "content", "") or "").strip():
            assistant_msg.metadata.update(
                {
                    "incomplete": True,
                    "completion": False,
                    "completion_policy": "text",
                    "interrupted": True,
                    "interrupt_reason": "empty_response",
                }
            )
            turn_context.state = TurnState.TURN_COMPLETE
            return TurnOutcome(
                kind=TurnOutcomeKind.INTERRUPTED,
                context=turn_context,
                final_message=assistant_msg,
                error="模型未返回可见文本。",
                stop_reason=RunStopReason.EMPTY_RESPONSE,
            )
        assistant_msg.metadata["completion"] = True
        assistant_msg.metadata["completion_policy"] = "text"
        turn_context.state = TurnState.TURN_COMPLETE
        return TurnOutcome(kind=TurnOutcomeKind.COMPLETE, context=turn_context, final_message=assistant_msg)

    async def _execute_tool_calls(
        self,
        *,
        provider: Provider,
        conversation: Conversation,
        policy: RunPolicy,
        turn_context: TurnContext,
        assistant_msg: Message,
        repetition_detector: ToolRepetitionDetector,
        emitter: EventEmitter,
        approval_callback,
        questions_callback,
        cancel_event,
        on_event,
        on_token,
        on_thinking,
        turns_limit: int,
        run_control: RunControl | None = None,
        debug_trace: DebugTraceContext | None = None,
        compression_tasks: CompressionTaskSet | None = None,
    ) -> TurnOutcome:
        del on_token, on_thinking
        return await self._build_tool_call_coordinator(compression_tasks).execute(
            provider=provider,
            conversation=conversation,
            policy=policy,
            turn_context=turn_context,
            assistant_msg=assistant_msg,
            repetition_detector=repetition_detector,
            emitter=emitter,
            approval_callback=approval_callback,
            questions_callback=questions_callback,
            cancel_event=cancel_event,
            on_event=on_event,
            turns_limit=turns_limit,
            run_control=run_control,
            debug_trace=debug_trace,
        )

    @staticmethod
    def _take_runtime_guidance(
        run_control: RunControl | None,
        *,
        terminal: bool,
        can_continue: bool,
    ) -> tuple[str, ...]:
        if run_control is None or not can_continue:
            return ()
        if terminal:
            return run_control.take_or_close()
        return run_control.take_pending()

    @staticmethod
    def _append_runtime_guidance(
        conversation: Conversation,
        guidance: tuple[str, ...],
        *,
        emitter: EventEmitter,
        turn: int,
    ) -> None:
        submitted_at = datetime.now().isoformat(timespec="seconds")
        for text in guidance:
            message = Message(
                role="user",
                content=text,
                metadata={
                    "runtime_guidance": True,
                    "submitted_at": submitted_at,
                },
            )
            conversation.add_message(message)
            emitter.emit(RunEventKind.STEP, turn=turn, data=message)
        emit_conversation_patch(
            emitter,
            conversation,
            turn=turn,
            detail="Runtime guidance appended.",
            include_messages=True,
        )

    def _build_tool_call_coordinator(
        self,
        compression_tasks: CompressionTaskSet | None = None,
    ) -> ToolCallCoordinator:
        return ToolCallCoordinator(
            client=self._client,
            tool_executor=self._tool_executor,
            subtask_coordinator=self._subtask_coordinator,
            tool_result_to_string=self._tool_result_to_string,
            build_tool_result_block=(
                self._build_tool_result_block
                if compression_tasks is None
                else lambda **kwargs: self._prepare_tool_result_block(
                    compression_tasks=compression_tasks,
                    **kwargs,
                )
            ),
            compression_tasks=compression_tasks,
        )

    def _build_max_turns_message(
        self,
        *,
        conversation: Conversation,
        final_assistant: Optional[Message],
    ) -> Optional[Message]:
        return TaskStopPolicy.build_max_turns_message(
            conversation=conversation,
            final_assistant=final_assistant,
            attach_state_snapshot=self._attach_state_snapshot,
        )

    def _build_tool_result_block(
        self,
        *,
        conversation: Conversation,
        tool_name: str,
        tool_category: str = "",
        tool_call_id: Optional[str],
        result: ToolResult | str,
        summary: Optional[str] = None,
        tool_args: Optional[dict[str, Any]] = None,
        parent_message_id: str = "",
    ) -> dict[str, Any]:
        return self._tool_result_recorder.build_block(
            conversation=conversation,
            tool_name=tool_name,
            tool_category=tool_category,
            tool_call_id=tool_call_id,
            result=result,
            summary=summary,
            tool_args=tool_args,
            parent_message_id=parent_message_id,
        )

    async def _prepare_tool_result_block(
        self,
        *,
        compression_tasks: CompressionTaskSet,
        conversation: Conversation,
        tool_name: str,
        tool_category: str = "",
        tool_call_id: Optional[str],
        result: ToolResult | str,
        summary: Optional[str] = None,
        tool_args: Optional[dict[str, Any]] = None,
        parent_message_id: str = "",
    ) -> dict[str, Any]:
        return await self._tool_result_recorder.prepare_block(
            conversation=conversation,
            tool_name=tool_name,
            tool_category=tool_category,
            tool_call_id=tool_call_id,
            result=result,
            summary=summary,
            tool_args=tool_args,
            parent_message_id=parent_message_id,
            compression_tasks=compression_tasks,
        )

    async def _get_or_create_archive_summary(
        self,
        *,
        content_id: str,
        provider: Provider,
        conversation: Conversation,
        debug_trace: DebugTraceContext | None,
    ):
        factory = self._compression_factory
        capability_executor = self._capability_executor
        if factory is None or capability_executor is None:
            return None
        try:
            capability = capability_executor.get_capability("compress")
        except Exception:
            return None
        if str(getattr(capability, "runtime", "single_turn") or "single_turn") != "single_turn":
            return None
        work_dir = str(getattr(conversation, "work_dir", "") or "")
        session_ids = [str(getattr(conversation, "id", "") or "")]
        settings = getattr(conversation, "settings", {}) or {}
        parent_id = str(settings.get("parent_session_id") or "") if isinstance(settings, dict) else ""
        if parent_id and parent_id not in session_ids:
            session_ids.append(parent_id)
        session_id = ""
        store = None
        for candidate in session_ids:
            candidate_store = SessionArchiveStore(work_dir, conversation_id=candidate or None)
            if candidate_store.read_record(content_id) is not None:
                store = candidate_store
                session_id = candidate
                break
        if store is None:
            return None
        compressor = factory(
            provider=provider,
            debug_trace=debug_trace.with_purpose("condense") if debug_trace is not None else None,
        )
        return await ArchiveViewService(
            work_dir=work_dir,
            conversation_id=session_id or None,
            conversation=conversation,
            compressor=compressor,
        ).get_or_create_summary(content_id, trace_purpose="tool_result:first_view")

    @staticmethod
    def _queue_missing_tool_summaries(
        conversation: Conversation,
        compression_tasks: CompressionTaskSet,
    ) -> None:
        work_dir = str(getattr(conversation, "work_dir", "") or "")
        store = SessionArchiveStore(work_dir, conversation_id=getattr(conversation, "id", None))
        for message in getattr(conversation, "messages", []) or []:
            if getattr(message, "archived_content_id", None):
                continue
            for tool_call in getattr(message, "tool_calls", None) or []:
                if not isinstance(tool_call, dict) or tool_call.get("result") is None:
                    continue
                payload = normalize_tool_result(tool_call.get("result"))
                metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
                content_id = str(metadata.get("content_id") or "").strip()
                if not content_id:
                    continue
                try:
                    chars = int(metadata.get("tool_result_chars") or metadata.get("archive_size") or 0)
                except Exception:
                    chars = 0
                if chars <= ToolResultViewService.SHORT_LIMIT:
                    continue
                record = store.read_record(content_id)
                if record is not None and not record.summary:
                    compression_tasks.submit(content_id, priority="background")

    @staticmethod
    def _tool_result_to_string(result: ToolResult | str) -> str:
        return ToolResultRecorder.tool_result_to_string(result)

    @staticmethod
    def _extract_tool_images(result: ToolResult | str) -> list[str]:
        return ToolResultRecorder.extract_tool_images(result)

    @staticmethod
    def _record_work_trace_step(
        *,
        state,
        conversation: Conversation,
        tool_name: str,
        tool_call_id: Optional[str],
        result_summary: str,
        metadata: dict[str, Any],
        handle,
    ) -> None:
        ToolResultRecorder.record_work_trace_step(
            state=state,
            conversation=conversation,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            result_summary=result_summary,
            metadata=metadata,
            handle=handle,
        )

    @staticmethod
    def _work_trace_label(tool_name: str) -> str:
        return ToolResultRecorder.work_trace_label(tool_name)

    @staticmethod
    def _work_trace_target(tool_name: str, metadata: dict[str, Any]) -> str:
        return ToolResultRecorder.work_trace_target(tool_name, metadata)

    @staticmethod
    def _latest_user_goal(conversation: Conversation) -> str:
        return ToolResultRecorder.latest_user_goal(conversation)

    @staticmethod
    def _summarize_tool_result(tool_name: str, result_text: str) -> str:
        lines = [line.strip() for line in str(result_text or "").splitlines() if line.strip()]
        if not lines:
            return f"{tool_name} completed."

        if tool_name in {"state__todo", "state__artifact", "state__memory"}:
            return lines[0][:220]

        if tool_name == "skill__load":
            for line in lines:
                if line.startswith("Skill:"):
                    return f"Loaded {line.split(':', 1)[1].strip()}."
            return "Skill loaded."

        if tool_name == "skill__read_resource":
            return lines[0][:220]

        return lines[0][:220]

    @staticmethod
    def _conversation_patch_payload(conversation: Conversation) -> dict[str, Any]:
        return conversation_patch_payload(conversation)

    def _emit_conversation_patch(
        self,
        emitter: EventEmitter | None,
        conversation: Conversation,
        *,
        turn: int = 0,
        detail: str = "Conversation state synchronized.",
        diagnostics: dict[str, Any] | None = None,
        include_messages: bool = True,
    ) -> None:
        emit_conversation_patch(
            emitter,
            conversation,
            turn=turn,
            detail=detail,
            diagnostics=diagnostics,
            include_messages=include_messages,
        )

    @staticmethod
    def _state_version(conversation: Conversation) -> int:
        try:
            return int(conversation.get_state().state_version or 0)
        except Exception:
            return 0

    def _attach_state_snapshot(self, conversation: Conversation, msg: Message) -> None:
        """Attach state snapshot to message."""
        self._tool_executor.attach_state_snapshot(conversation, msg)
