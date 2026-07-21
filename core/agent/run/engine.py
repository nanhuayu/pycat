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
from typing import Any, Callable, Optional

from models.contracts.config import AppConfig
from models.conversation import Conversation, Message
from models.provider import Provider

from core.llm.client import LLMClient
from core.prompts.renderer import PromptRenderer
from core.tools.manager import ToolManager
from core.tools.base import ToolResult
from core.context.maintainer import ContextMaintainer
from core.capabilities.compression import CapabilityCompressionOrchestrator
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
from core.agent.request.retry import ErrorKind, classify_error
from core.agent.tooling.repetition import ToolRepetitionDetector
from core.agent.request.pipeline import RequestPipeline
from core.agent.tooling.executor import ToolExecutor
from core.agent.events.emitter import EventEmitter
from core.agent.events.conversation import (
    conversation_patch_payload,
    emit_condense_report,
    emit_conversation_patch,
)
from core.agent.delegation.subagent import build_root_run
from core.agent.run.stop_policy import TaskStopPolicy
from core.agent.delegation.runner import SubagentRunner
from core.agent.tooling.loop import ToolCallCoordinator
from core.agent.tooling.result_recorder import ToolResultRecorder
from core.agent.events.debug_trace import DebugTraceContext, ensure_debug_trace
from core.agent.run.control_messages import EXPLICIT_COMPLETION_REMINDER, MAX_EXPLICIT_COMPLETION_RETRIES
from core.memory.advisor import MemoryAdvisor

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
    ) -> None:
        self._client = client
        self._tool_manager = tool_manager
        self._app_config = app_config or AppConfig()
        self._app_settings = self._app_config.to_dict()
        compressor_factory = self._compressor_factory(capability_executor)
        self._memory_advisor = MemoryAdvisor(capability_executor) if capability_executor is not None else None
        self._context_maintenance = ContextMaintainer(
            client=client,
            app_config=self._app_config,
            compressor_factory=compressor_factory,
        )
        self._llm_executor = RequestPipeline(
            client,
            tool_manager=tool_manager,
            context_maintenance=self._context_maintenance,
            prompt_renderer=prompt_renderer,
            memory_advisor=self._memory_advisor,
            app_config=self._app_config,
        )
        self._tool_executor = ToolExecutor(
            tool_manager,
            capability_executor=capability_executor,
            archive_compressor_factory=compressor_factory,
            shell_config=getattr(self._app_config, "shell", None),
        )
        self._subtask_coordinator = SubagentRunner(
            self,
            provider_catalog_provider=provider_catalog_provider,
        )
        self._tool_result_recorder = ToolResultRecorder(
            summarize_tool_result=self._summarize_tool_result,
            client=client,
            archive_compressor_factory=compressor_factory,
        )
        self._pre_turn_hooks: list[Callable] = []
        self._post_turn_hooks: list[Callable] = []

    @staticmethod
    def _compressor_factory(capability_executor: Any):
        if capability_executor is None:
            return None

        def factory(*, client: Any, provider: Provider, store: Any, debug_trace: Any = None):
            return CapabilityCompressionOrchestrator(
                client,
                provider,
                store=store,
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
        approval_callback: Optional[Callable[[str], bool]] = None,
        questions_callback: Optional[Callable[[dict[str, Any]], Any]] = None,
        cancel_event: Optional[threading.Event] = None,
        debug_log_path: Optional[str] = None,
        debug_trace: DebugTraceContext | None = None,
        initial_runtime_messages: Optional[list[Message]] = None,
    ) -> RunResult:
        run = build_root_run(conversation=conversation, provider=provider, policy=policy)
        provider = run.provider
        conversation = run.conversation
        policy = run.policy
        turns_limit = max(1, int(policy.max_turns or 200))
        final_assistant: Optional[Message] = None
        turn_context = TurnContext(turn=0, runtime_messages=list(initial_runtime_messages or []))
        repetition_detector = ToolRepetitionDetector()
        emitter = EventEmitter(on_event)
        trace_context = ensure_debug_trace(debug_trace)

        for turn in range(turns_limit):
            turn_context.turn = turn + 1
            turn_trace = self._turn_trace_context(trace_context, turn_context.turn)
            outcome = await self._run_turn(
                provider=provider,
                conversation=conversation,
                policy=policy,
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
            )
            self._record_turn_end(turn_trace, outcome)
            turn_context = outcome.context
            if outcome.final_message is not None:
                final_assistant = outcome.final_message
            if outcome.next_policy is not None:
                policy = outcome.next_policy

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
            await self._curate_memory(
                provider=provider,
                conversation=conversation,
                policy=policy,
                final_message=final_message,
                debug_trace=turn_trace,
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
        debug_trace: DebugTraceContext | None = None,
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
        )
        if isinstance(assistant_msg, TurnOutcome):
            return assistant_msg

        turn_context.runtime_messages = []
        turn_context.state = TurnState.ASSISTANT_RECEIVED
        conversation.add_message(assistant_msg)
        emitter.emit(RunEventKind.STEP, turn=turn_context.turn, data=assistant_msg)

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
            debug_trace=debug_trace,
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
                emit=lambda **kw: emitter.emit(turn=turn_context.turn, **kw),
            )
        except Exception as e:
            kind = classify_error(e)
            if kind == ErrorKind.CONTEXT_OVERFLOW:
                try:
                    report = await self._force_condense(
                        conversation,
                        provider,
                        policy,
                        debug_trace=debug_trace.with_purpose("condense") if debug_trace is not None else None,
                        turn=turn_context.turn,
                    )
                    self._emit_condense_report(emitter, conversation, report, turn=turn_context.turn)
                    return await self._llm_executor._call_raw(
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
                        debug_purpose="retry",
                    )
                except Exception as e2:
                    turn_context.state = TurnState.FAILED
                    return TurnOutcome(
                        kind=TurnOutcomeKind.FAILED,
                        context=turn_context,
                        error=str(e2),
                        stop_reason=RunStopReason.ERROR,
                    )
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
        debug_trace: DebugTraceContext | None = None,
    ) -> TurnOutcome:
        del on_token, on_thinking
        return await self._build_tool_call_coordinator().execute(
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
            debug_trace=debug_trace,
        )

    def _build_tool_call_coordinator(self) -> ToolCallCoordinator:
        return ToolCallCoordinator(
            client=self._client,
            tool_executor=self._tool_executor,
            subtask_coordinator=self._subtask_coordinator,
            tool_result_to_string=self._tool_result_to_string,
            summarize_tool_result=self._summarize_tool_result,
            build_tool_result_block=self._build_tool_result_block_async,
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
        **kwargs,
    ) -> dict[str, Any]:
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._build_tool_result_block_async(**kwargs))
        raise RuntimeError("_build_tool_result_block cannot be called synchronously from a running event loop")

    async def _build_tool_result_block_async(
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
        provider: Provider | None = None,
        debug_trace: DebugTraceContext | None = None,
        on_archive_prepare: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        return await self._tool_result_recorder.build_block(
            conversation=conversation,
            tool_name=tool_name,
            tool_category=tool_category,
            tool_call_id=tool_call_id,
            result=result,
            summary=summary,
            tool_args=tool_args,
            parent_message_id=parent_message_id,
            provider=provider,
            debug_trace=debug_trace,
            on_archive_prepare=on_archive_prepare,
        )

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

    # ------------------------------------------------------------------
    # Context management (condense)
    # ------------------------------------------------------------------

    async def _force_condense(
        self,
        conversation: Conversation,
        provider: Provider,
        policy: RunPolicy,
        *,
        debug_trace: DebugTraceContext | None = None,
        turn: int = 0,
    ):
        """Emergency condense on context overflow."""
        report = await self._context_maintenance.maintain_async(
            conversation,
            provider=provider,
            policy=policy,
            client=self._client,
            force=True,
            honor_auto_enabled=False,
            keep_last_turns=3,
            token_threshold_ratio=0.80,
            debug_trace=debug_trace,
        )
        logger.info("Emergency condense complete")
        return report

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
    ) -> None:
        emit_conversation_patch(
            emitter,
            conversation,
            turn=turn,
            detail=detail,
            diagnostics=diagnostics,
        )

    def _emit_condense_report(
        self,
        emitter: EventEmitter | None,
        conversation: Conversation,
        report,
        *,
        turn: int = 0,
        live_message_id: str = "",
        live_tool_call_id: str = "",
    ) -> None:
        emit_condense_report(
            emitter,
            conversation,
            report,
            turn=turn,
            live_message_id=live_message_id,
            live_tool_call_id=live_tool_call_id,
        )

    def _attach_state_snapshot(self, conversation: Conversation, msg: Message) -> None:
        """Attach state snapshot to message."""
        self._tool_executor.attach_state_snapshot(conversation, msg)
