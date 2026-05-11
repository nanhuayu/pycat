"""Unified task execution engine - main coordinator.

Simplified from 574 lines to ~200 lines by extracting:
- LLMExecutor: LLM API calls with retry
- ToolExecutor: Tool execution and state management
- EventEmitter: Event streaming

This module now focuses on orchestration:
- Turn-based execution loop
- Hook management
- Auto-continue logic
- Sub-task delegation
"""
from __future__ import annotations

from dataclasses import replace
import logging
import threading
import time
import uuid
from typing import Any, Callable, Optional
from core.tools.catalog import ToolSelectionPolicy

from models.conversation import Conversation, Message
from models.conversation import get_tool_call_result, normalize_tool_result, set_tool_call_result
from models.provider import Provider

from core.llm.client import LLMClient
from core.tools.manager import ToolManager
from core.tools.base import ToolResult
from core.tools.result_pipeline import ToolResultPipeline
from core.config import load_app_config, AppConfig
from core.task.types import (
    RunPolicy,
    SubTaskOutcome,
    SubtaskTrace,
    SubtaskTraceStatus,
    TaskEvent,
    TaskEventKind,
    TaskResult,
    TaskStatus,
    TaskTurnState,
    TurnContext,
    TurnOutcome,
    TurnOutcomeKind,
)
from core.task.retry import ErrorKind, classify_error
from core.task.repetition import ToolRepetitionDetector
from core.task.executor import LLMExecutor
from core.task.tool_executor import ToolExecutor
from core.task.event_emitter import EventEmitter

logger = logging.getLogger(__name__)

# Modes where the agent should auto-continue when LLM returns text
# without tool calls (nudge it to keep working or call attempt_completion).
_AUTO_CONTINUE_MODES = frozenset({"agent", "code", "debug", "plan", "orchestrator"})
_MAX_NUDGE_COUNT = 3

_MODE_SWITCH_MESSAGE = (
    "[MODE SWITCHED] The conversation mode has changed. "
    "Continue under the new mode's responsibilities, tool set, and completion rules."
)

_NUDGE_TEXT = (
    "[AUTO-CONTINUE] You responded without using any tools. "
    "If you have not completed the task, please continue using the available tools. "
    "If the task is complete, call the `attempt_completion` tool to present your result. "
    "If the user asked for a document, report, timeline, or artifact, create/update it first and include the path in `attempt_completion`. "
    "If a tool result says it was stored in a full-result file, do not repeatedly slice the same source; use `read_file`, "
    "call `capability__summarize_text` for one file or one long text, or delegate multi-file/cross-source analysis with `subagent__read_analyze`. "
    "Do not simply describe what you would do — take action."
)

_FINALIZE_TEXT = (
    "[FINALIZE NOW] This is the final available turn for this task loop. "
    "Stop collecting more evidence unless absolutely required. Consolidate the facts already gathered, "
    "create or update any requested report/document/artifact, and then call `attempt_completion` with the final answer and file paths. "
    "Do not start a new broad search or repeat previous tool calls."
)

_REPETITION_WARNING = (
    "[WARNING] You have called the same tool with identical arguments multiple times consecutively. "
    "This is not making progress. Please try a different approach, use different arguments, "
    "summarize a single long source with `capability__summarize_text`, delegate complex evidence with `subagent__custom` or `subagent__read_analyze`, "
    "or call `attempt_completion` if done."
)

class Task:
    """Unified think-act tool loop coordinator.

    Orchestrates LLM calls, tool execution, and event streaming.
    Delegates to specialized executors for each responsibility.
    """

    def __init__(
        self,
        *,
        client: LLMClient,
        tool_manager: ToolManager,
    ) -> None:
        self._client = client
        self._tool_manager = tool_manager
        self._llm_executor = LLMExecutor(client)
        self._tool_executor = ToolExecutor(tool_manager)
        self._pre_turn_hooks: list[Callable] = []
        self._post_turn_hooks: list[Callable] = []
        self._result_pipeline: ToolResultPipeline | None = None

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
        on_event: Optional[Callable[[TaskEvent], None]] = None,
        on_token: Optional[Callable[[str], None]] = None,
        on_thinking: Optional[Callable[[str], None]] = None,
        approval_callback: Optional[Callable[[str], bool]] = None,
        questions_callback: Optional[Callable[[dict[str, Any]], Any]] = None,
        cancel_event: Optional[threading.Event] = None,
        debug_log_path: Optional[str] = None,
    ) -> TaskResult:
        turns_limit = max(1, int(policy.max_turns or 200))
        final_assistant: Optional[Message] = None
        turn_context = TurnContext(turn=0)
        repetition_detector = ToolRepetitionDetector()
        emitter = EventEmitter(on_event)

        for turn in range(turns_limit):
            turn_context.turn = turn + 1
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
                on_event=on_event,
            )
            turn_context = outcome.context
            if outcome.final_message is not None:
                final_assistant = outcome.final_message
            if outcome.next_policy is not None:
                policy = outcome.next_policy

            if outcome.kind == TurnOutcomeKind.CONTINUE:
                continue
            if outcome.kind == TurnOutcomeKind.CANCELLED:
                return TaskResult(status=TaskStatus.CANCELLED, final_message=final_assistant)
            if outcome.kind == TurnOutcomeKind.FAILED:
                return TaskResult(status=TaskStatus.FAILED, final_message=final_assistant, error=outcome.error)
            return TaskResult(status=TaskStatus.COMPLETED, final_message=outcome.final_message or final_assistant)

        fallback = self._build_max_turns_message(conversation=conversation, final_assistant=final_assistant)
        if fallback is not None:
            emitter.emit(TaskEventKind.STEP, turn=turns_limit, data=fallback)
            emitter.emit(TaskEventKind.COMPLETE, turn=turns_limit, data=fallback)
            conversation.add_message(fallback)
            return TaskResult(status=TaskStatus.COMPLETED, final_message=fallback)
        return TaskResult(status=TaskStatus.COMPLETED, final_message=final_assistant)

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
    ) -> TurnOutcome:
        if cancel_event and cancel_event.is_set():
            turn_context.state = TaskTurnState.CANCELLED
            return TurnOutcome(kind=TurnOutcomeKind.CANCELLED, context=turn_context)

        emitter.emit(TaskEventKind.TURN_START, turn=turn_context.turn, detail=f"Turn {turn_context.turn}/{turns_limit}")
        turn_context.state = TaskTurnState.PRE_TURN_HOOKS
        for hook in self._pre_turn_hooks:
            try:
                hook(conversation, turn_context.turn, policy)
            except Exception as he:
                logger.debug("Pre-turn hook error: %s", he)

        turn_context.state = TaskTurnState.CONDENSING
        await self._maybe_condense(conversation, provider, policy)

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
        )
        if isinstance(assistant_msg, TurnOutcome):
            return assistant_msg

        turn_context.runtime_messages = []
        turn_context.state = TaskTurnState.ASSISTANT_RECEIVED
        try:
            assistant_msg.seq_id = conversation.next_seq_id()
        except Exception as e:
            logger.warning("Failed to assign seq_id to assistant message: %s", e)

        emitter.emit(TaskEventKind.STEP, turn=turn_context.turn, data=assistant_msg)
        conversation.add_message(assistant_msg)

        for hook in self._post_turn_hooks:
            try:
                hook(conversation, turn_context.turn, assistant_msg)
            except Exception as he:
                logger.debug("Post-turn hook error: %s", he)

        if cancel_event and cancel_event.is_set():
            turn_context.state = TaskTurnState.CANCELLED
            return TurnOutcome(kind=TurnOutcomeKind.CANCELLED, context=turn_context, final_message=assistant_msg)

        if not assistant_msg.tool_calls:
            return await self._handle_turn_without_tools(
                provider=provider,
                conversation=conversation,
                policy=policy,
                turn_context=turn_context,
                assistant_msg=assistant_msg,
            )

        turn_context.nudge_count = 0
        turn_context.state = TaskTurnState.TOOL_EXECUTION
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
    ) -> Message | TurnOutcome:
        turn_context.state = TaskTurnState.LLM_CALL
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
                emit=lambda **kw: emitter.emit(turn=turn_context.turn, **kw),
            )
        except Exception as e:
            kind = classify_error(e)
            if kind == ErrorKind.CONTEXT_OVERFLOW:
                try:
                    await self._force_condense(conversation, provider, policy)
                    return await self._llm_executor._call_raw(
                        provider=provider,
                        conversation=conversation,
                        policy=policy,
                        runtime_messages=turn_context.runtime_messages,
                        on_token=on_token,
                        on_thinking=on_thinking,
                        cancel_event=cancel_event,
                        debug_log_path=debug_log_path,
                    )
                except Exception as e2:
                    turn_context.state = TaskTurnState.FAILED
                    return TurnOutcome(kind=TurnOutcomeKind.FAILED, context=turn_context, error=str(e2))
            turn_context.state = TaskTurnState.FAILED
            return TurnOutcome(kind=TurnOutcomeKind.FAILED, context=turn_context, error=str(e))

    async def _handle_turn_without_tools(
        self,
        *,
        provider: Provider,
        conversation: Conversation,
        policy: RunPolicy,
        turn_context: TurnContext,
        assistant_msg: Message,
    ) -> TurnOutcome:
        mode_slug = (policy.mode or "chat").lower()
        if mode_slug in _AUTO_CONTINUE_MODES and turn_context.nudge_count < _MAX_NUDGE_COUNT:
            turn_context.nudge_count += 1
            logger.info(
                "Auto-continue nudge %d/%d (mode=%s)",
                turn_context.nudge_count,
                _MAX_NUDGE_COUNT,
                mode_slug,
            )
            turn_context.runtime_messages = [Message(role="user", content=_NUDGE_TEXT)]
            turn_context.state = TaskTurnState.TURN_COMPLETE
            return TurnOutcome(kind=TurnOutcomeKind.CONTINUE, context=turn_context, final_message=assistant_msg)

        await self._maybe_condense(conversation, provider, policy)
        self._attach_state_snapshot(conversation, assistant_msg)
        turn_context.state = TaskTurnState.TURN_COMPLETE
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
    ) -> TurnOutcome:
        total_tools = len(assistant_msg.tool_calls or [])
        for tool_call in assistant_msg.tool_calls or []:
            if cancel_event and cancel_event.is_set():
                turn_context.state = TaskTurnState.CANCELLED
                return TurnOutcome(kind=TurnOutcomeKind.CANCELLED, context=turn_context, final_message=assistant_msg)

            tool_name, args, tool_call_id = self._tool_executor.parse_tool_call(tool_call)
            tool_event_base = {
                "tool_name": tool_name,
                "tool_call_id": tool_call_id or "",
                "phase": "start",
                "allowed": None,
                "is_error": False,
                "total_tools": total_tools,
            }
            if repetition_detector.record(tool_name, args if isinstance(args, dict) else {}):
                logger.warning("Tool repetition detected: %s", tool_name)
                emitter.emit(
                    TaskEventKind.TOOL_END,
                    turn=turn_context.turn,
                    detail=f"Tool {tool_name} stopped: repeated identical arguments.",
                    data={**tool_event_base, "phase": "repetition", "is_error": True},
                )
                turn_context.runtime_messages = [Message(role="user", content=_REPETITION_WARNING)]
                repetition_detector.reset()
                turn_context.state = TaskTurnState.TURN_COMPLETE
                return TurnOutcome(kind=TurnOutcomeKind.CONTINUE, context=turn_context, final_message=assistant_msg)

            allowed = self._tool_executor.is_tool_allowed(tool_name, policy)
            emitter.emit(
                TaskEventKind.TOOL_START,
                turn=turn_context.turn,
                detail=f"Tool {tool_name} started.",
                data={**tool_event_base, "allowed": bool(allowed)},
            )
            context = self._tool_executor.build_tool_context(
                conversation=conversation,
                provider=provider,
                approval_callback=approval_callback,
                questions_callback=questions_callback,
                llm_client=self._client,
            )
            tool_result = await self._tool_executor.execute_tool(
                tool_name=tool_name,
                tool_args=args,
                allowed=allowed,
                policy=policy,
                context=context,
            )
            result_text = self._tool_result_to_string(tool_result)
            tool_is_error = bool(getattr(tool_result, "is_error", False))

            subtask_req = (context.state or {}).get("_pending_subtask")
            if subtask_req and isinstance(subtask_req, dict):
                context.state.pop("_pending_subtask", None)
                if not subtask_req.get("id"):
                    subtask_req["id"] = f"subtask-{uuid.uuid4().hex[:12]}"
                preview_trace = SubtaskTrace(
                    id=str(subtask_req.get("id") or ""),
                    kind=str(subtask_req.get("kind") or "subagent"),
                    name=self._subtask_display_name(subtask_req, str(subtask_req.get("mode") or "agent")),
                    title=self._subtask_display_name(subtask_req, str(subtask_req.get("mode") or "agent")),
                    goal=self._subtask_goal(subtask_req),
                    mode=str(subtask_req.get("mode") or "agent"),
                    depth=int(subtask_req.get("depth") or 0),
                    metadata={
                        "capability_id": str(subtask_req.get("capability_id") or ""),
                        "capabilities": list(subtask_req.get("capabilities") or []),
                        "tool_call_id": tool_call_id or "",
                        "parent_message_id": str(getattr(assistant_msg, "id", "") or ""),
                        "parent_tool_call_id": tool_call_id or "",
                        "root_tool_call_id": tool_call_id or "",
                    },
                )
                if tool_call_id:
                    subtask_req["tool_call_id"] = tool_call_id
                    subtask_req["parent_message_id"] = str(getattr(assistant_msg, "id", "") or "")
                    subtask_req["parent_tool_call_id"] = tool_call_id
                    subtask_req["root_tool_call_id"] = tool_call_id
                    self._attach_subtask_trace(assistant_msg, tool_call_id, preview_trace)
                    self._publish_subtask_trace_event(
                        on_event,
                        preview_trace,
                        turn=turn_context.turn,
                        detail=f"Subtask {preview_trace.title} started.",
                    )
                subtask_outcome = await self._run_subtask(
                    subtask_req=subtask_req,
                    provider=provider,
                    conversation=conversation,
                    approval_callback=approval_callback,
                    questions_callback=questions_callback,
                    cancel_event=cancel_event,
                    on_event=on_event,
                    turn=turn_context.turn,
                )
                self._attach_subtask_trace(assistant_msg, tool_call_id, subtask_outcome.trace)
                tool_result = ToolResult(subtask_outcome.message)
                result_text = self._tool_result_to_string(tool_result)
                tool_is_error = bool(getattr(tool_result, "is_error", False))

            emitter.emit(
                TaskEventKind.TOOL_END,
                turn=turn_context.turn,
                detail=f"Tool {tool_name} {'failed' if tool_is_error else 'completed'}.",
                data={
                    **tool_event_base,
                    "phase": "end",
                    "allowed": bool(allowed),
                    "is_error": tool_is_error,
                    "summary": self._summarize_tool_result(tool_name, result_text),
                },
            )

            if (context.state or {}).get("_task_completed"):
                completion_result = str((context.state or {}).get("_completion_result") or result_text or "").strip()
                completion_command = str((context.state or {}).get("_completion_command") or "").strip()
                self._tool_executor.sync_state(conversation, context)
                result_block = self._build_tool_result_block(
                    conversation=conversation,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    result=tool_result,
                    summary="Completion acknowledged.",
                )
                emitter.emit(TaskEventKind.STEP, turn=turn_context.turn, data=result_block["event"])
                conversation.attach_tool_result(
                    tool_call_id,
                    result_block["result"],
                    summary=str(result_block["result"].get("summary") or ""),
                    metadata=dict(result_block["result"].get("metadata") or {}),
                    images=list(result_block.get("images") or []),
                    state_snapshot=result_block.get("state_snapshot") if isinstance(result_block.get("state_snapshot"), dict) else None,
                )

                final_msg = self._build_completion_message(
                    conversation=conversation,
                    completion_text=completion_result or "Task completed.",
                    completion_command=completion_command,
                )
                emitter.emit(TaskEventKind.STEP, turn=turn_context.turn, data=final_msg)
                emitter.emit(TaskEventKind.COMPLETE, turn=turn_context.turn, data=final_msg)
                conversation.add_message(final_msg)
                turn_context.state = TaskTurnState.TURN_COMPLETE
                return TurnOutcome(kind=TurnOutcomeKind.COMPLETE, context=turn_context, final_message=final_msg)

            self._tool_executor.sync_state(conversation, context)
            result_block = self._build_tool_result_block(
                conversation=conversation,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                result=tool_result,
            )

            switched_mode = str((context.state or {}).pop("_mode_switch", "") or "").strip().lower()
            next_policy = None
            if switched_mode:
                next_policy = self._build_switched_policy(
                    current_policy=policy,
                    conversation=conversation,
                    next_mode=switched_mode,
                )
                conversation.mode = switched_mode
                try:
                    result_block["result"].setdefault("metadata", {})["mode_switch"] = switched_mode
                    result_block["event"].setdefault("metadata", {})["mode_switch"] = switched_mode
                except Exception as exc:
                    logger.debug("Failed to annotate tool result with mode switch: %s", exc)

            emitter.emit(TaskEventKind.STEP, turn=turn_context.turn, data=result_block["event"])
            conversation.attach_tool_result(
                tool_call_id,
                result_block["result"],
                summary=str(result_block["result"].get("summary") or ""),
                metadata=dict(result_block["result"].get("metadata") or {}),
                images=list(result_block.get("images") or []),
                state_snapshot=result_block.get("state_snapshot") if isinstance(result_block.get("state_snapshot"), dict) else None,
            )

            if next_policy is not None:
                turn_context.nudge_count = 0
                turn_context.runtime_messages = [Message(role="user", content=_MODE_SWITCH_MESSAGE)]
                turn_context.state = TaskTurnState.TURN_COMPLETE
                return TurnOutcome(
                    kind=TurnOutcomeKind.CONTINUE,
                    context=turn_context,
                    final_message=assistant_msg,
                    next_policy=next_policy,
                )

        turn_context.state = TaskTurnState.TURN_COMPLETE
        if self._should_finalize_next_turn(policy=policy, turn_context=turn_context, turns_limit=turns_limit):
            turn_context.runtime_messages = [Message(role="user", content=_FINALIZE_TEXT)]
        else:
            turn_context.runtime_messages = []
        return TurnOutcome(kind=TurnOutcomeKind.CONTINUE, context=turn_context, final_message=assistant_msg)

    def _should_finalize_next_turn(
        self,
        *,
        policy: RunPolicy,
        turn_context: TurnContext,
        turns_limit: int,
    ) -> bool:
        mode_slug = str(getattr(policy, "mode", "") or "").strip().lower()
        if mode_slug not in _AUTO_CONTINUE_MODES:
            return False
        return int(turn_context.turn or 0) >= max(1, int(turns_limit or 1) - 1)

    def _build_max_turns_message(
        self,
        *,
        conversation: Conversation,
        final_assistant: Optional[Message],
    ) -> Optional[Message]:
        if final_assistant is not None and str(getattr(final_assistant, "content", "") or "").strip():
            return final_assistant

        latest_tool_summary = ""
        try:
            for msg in reversed(getattr(conversation, "messages", []) or []):
                if getattr(msg, "role", "") != "assistant":
                    continue
                for tool_call in reversed(getattr(msg, "tool_calls", None) or []):
                    if not isinstance(tool_call, dict) or "result" not in tool_call:
                        continue
                    result = normalize_tool_result(tool_call.get("result"))
                    latest_tool_summary = str(result.get("summary") or result.get("content") or "").strip()
                    if latest_tool_summary:
                        break
                if latest_tool_summary:
                    break
        except Exception:
            latest_tool_summary = ""

        content = (
            "任务循环已达到最大轮数，未收到 `attempt_completion`。"
            "请根据当前已收集的信息继续发起一次请求，或提高最大轮数后重试。"
        )
        if latest_tool_summary:
            content += f"\n\n最后一次工具结果摘要：{latest_tool_summary[:500]}"

        final_msg = Message(role="assistant", content=content)
        final_msg.summary = content[:240]
        try:
            final_msg.metadata["completion"] = False
            final_msg.metadata["max_turns_reached"] = True
            final_msg.seq_id = conversation.next_seq_id()
        except Exception as exc:
            logger.debug("Failed to annotate max-turns message: %s", exc)
        self._attach_state_snapshot(conversation, final_msg)
        return final_msg

    def _build_tool_result_block(
        self,
        *,
        conversation: Conversation,
        tool_name: str,
        tool_call_id: Optional[str],
        result: ToolResult | str,
        summary: Optional[str] = None,
    ) -> dict[str, Any]:
        result_text = self._tool_result_to_string(result)

        # Lazy-init pipeline per conversation session. Tool outputs belong to
        # the same workspace session as artifacts/state instead of the process cwd.
        pipeline_work_dir = str(getattr(conversation, "work_dir", "") or ".")
        pipeline_session_id = str(getattr(conversation, "id", "") or "default")
        pipeline_key = (str(pipeline_work_dir), str(pipeline_session_id))
        current_key = getattr(self._result_pipeline, "_pycat_key", None)
        if self._result_pipeline is None or current_key != pipeline_key:
            work_dir = str(getattr(conversation, "work_dir", "") or ".")
            self._result_pipeline = ToolResultPipeline(work_dir, conversation_id=getattr(conversation, "id", None))
            try:
                setattr(self._result_pipeline, "_pycat_key", pipeline_key)
            except Exception:
                pass

        handle = self._result_pipeline.process(
            tool_name=tool_name,
            raw_text=result_text,
            tool_call_id=tool_call_id,
            seq_id=int(conversation.current_seq_id() or 0) + 1,
        )

        tool_images = self._extract_tool_images(result)
        metadata: dict[str, Any] = {"name": tool_name}

        try:
            subtask = self._subtask_for_tool_call(conversation, tool_call_id)
            if subtask and (str(tool_name or '').startswith('subagent__') or str(tool_name or '').startswith('capability__')):
                metadata["subtask_status"] = str(subtask.get("status") or "")
                metadata["subtask_summary"] = str(subtask.get("final_message") or subtask.get("error") or subtask.get("goal") or "")[:220]
            if handle.full_path:
                metadata["tool_result_file"] = handle.full_path
                metadata["tool_result_chars"] = handle.total_chars
                metadata["tool_result_truncated"] = True
            if handle.hint:
                metadata["tool_result_hint"] = handle.hint
            if handle.is_processed:
                metadata["tool_result_strategy"] = handle.strategy
        except Exception as e:
            logger.debug("Failed to set tool metadata: %s", e)

        try:
            result_summary = summary or self._summarize_tool_result(tool_name, handle.display)
        except Exception as e:
            logger.debug("Failed to summarize tool result for %s: %s", tool_name, e)
            result_summary = summary or ""

        state_snapshot: dict[str, Any] | None = None
        try:
            state_snapshot = conversation.get_state().to_dict()
        except Exception as exc:
            logger.debug("Failed to snapshot state for tool result: %s", exc)

        result_payload: dict[str, Any] = {
            "type": "tool_result",
            "content": handle.display,
            "summary": result_summary,
            "metadata": metadata,
        }
        if tool_images:
            result_payload["images"] = list(tool_images)

        return {
            "tool_call_id": str(tool_call_id or ""),
            "tool_name": tool_name,
            "result": result_payload,
            "images": list(tool_images),
            "state_snapshot": state_snapshot,
            "event": {
                "role": "tool_result",
                "tool_call_id": str(tool_call_id or ""),
                "tool_name": tool_name,
                "summary": result_summary,
                "metadata": dict(metadata),
            },
        }

    @staticmethod
    def _is_max_turns_failure_message(message: str, metadata: dict[str, Any] | None = None) -> bool:
        if metadata and metadata.get("max_turns_reached"):
            return True
        text = str(message or "")
        return "任务循环已达到最大轮数" in text or "未收到 `attempt_completion`" in text

    @staticmethod
    def _tool_result_to_string(result: ToolResult | str) -> str:
        if isinstance(result, ToolResult):
            return result.to_string()
        return str(result)

    @staticmethod
    def _extract_tool_images(result: ToolResult | str) -> list[str]:
        if not isinstance(result, ToolResult):
            return []
        content = getattr(result, "content", None)
        if not isinstance(content, list):
            return []

        images: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if str(block.get("type") or "") != "image":
                continue
            image_data = str(block.get("data") or "").strip()
            mime_type = str(block.get("mimeType") or "image/png").strip() or "image/png"
            if not image_data:
                continue
            if image_data.startswith(("data:", "http://", "https://")):
                images.append(image_data)
                continue
            images.append(f"data:{mime_type};base64,{image_data}")
        return images

    def _build_completion_message(
        self,
        *,
        conversation: Conversation,
        completion_text: str,
        completion_command: str = "",
    ) -> Message:
        final_msg = Message(role="assistant", content=(completion_text or "Task completed.").strip())
        if len(final_msg.content) > 240:
            final_msg.summary = final_msg.content[:237] + "..."
        else:
            final_msg.summary = final_msg.content

        try:
            final_msg.metadata["completion"] = True
            if completion_command:
                final_msg.metadata["completion_command"] = completion_command
            final_msg.seq_id = conversation.next_seq_id()
        except Exception as e:
            logger.debug("Failed to annotate completion message: %s", e)

        self._attach_state_snapshot(conversation, final_msg)
        return final_msg

    @staticmethod
    def _summarize_tool_result(tool_name: str, result_text: str) -> str:
        lines = [line.strip() for line in str(result_text or "").splitlines() if line.strip()]
        if not lines:
            return f"{tool_name} completed."

        if tool_name == "manage_state":
            useful = [
                line.lstrip("-•✅❌⚠️ ")
                for line in lines
                if line not in {"State updated:"} and not line.startswith(("📋", "💾", "📚"))
            ]
            if useful:
                return ("State updated: " + "; ".join(useful[:2]))[:220]
            return "State updated."

        if tool_name in {"manage_todo", "manage_artifact"}:
            return lines[0][:220]

        if tool_name == "skill__load":
            for line in lines:
                if line.startswith("Skill:"):
                    return f"Loaded {line.split(':', 1)[1].strip()}."
            return "Skill loaded."

        if tool_name == "skill__read_resource":
            return lines[0][:220]

        if tool_name == "attempt_completion":
            return "Completion acknowledged."

        return lines[0][:220]

    @staticmethod
    def _attach_subtask_trace(
        assistant_msg: Message,
        tool_call_id: str | None,
        trace: SubtaskTrace | None,
    ) -> None:
        if trace is None:
            return
        try:
            payload = trace.to_dict()
            if tool_call_id:
                payload["tool_call_id"] = tool_call_id
                payload["parent_tool_call_id"] = tool_call_id
                payload["root_tool_call_id"] = tool_call_id
            parent_message_id = ""
            try:
                parent_message_id = str(getattr(assistant_msg, "id", "") or "")
            except Exception:
                parent_message_id = ""
            if parent_message_id:
                payload["parent_message_id"] = parent_message_id
            metadata_payload = dict(payload.get("metadata") or {})
            if tool_call_id:
                metadata_payload.setdefault("tool_call_id", tool_call_id)
                metadata_payload.setdefault("parent_tool_call_id", tool_call_id)
                metadata_payload.setdefault("root_tool_call_id", tool_call_id)
            if parent_message_id:
                metadata_payload.setdefault("parent_message_id", parent_message_id)
            payload["metadata"] = metadata_payload
            assistant_msg.metadata = assistant_msg.metadata or {}
            assistant_msg.metadata.pop("subtasks", None)
            assistant_msg.metadata.pop("subtasks_by_call", None)
            if tool_call_id and assistant_msg.tool_calls:
                for tool_call in assistant_msg.tool_calls:
                    if tool_call.get("id") != tool_call_id:
                        continue
                    current = get_tool_call_result(tool_call)
                    metadata = dict(current.get("metadata") or {})
                    if tool_call_id:
                        metadata["tool_call_id"] = tool_call_id
                        metadata["parent_tool_call_id"] = tool_call_id
                        metadata["root_tool_call_id"] = str(metadata.get("root_tool_call_id") or tool_call_id)
                    if parent_message_id:
                        metadata["parent_message_id"] = parent_message_id
                    result_payload = {
                        "type": "subtask_run",
                        "content": str(trace.final_message or trace.error or trace.goal or ""),
                        "summary": str(trace.final_message or trace.error or trace.goal or "")[:220],
                        "metadata": metadata,
                        "run": payload,
                    }
                    set_tool_call_result(tool_call, result_payload)
                    break
        except Exception as exc:
            logger.debug("Failed to attach subtask trace: %s", exc)

    @staticmethod
    def _publish_subtask_trace_event(
        on_event,
        trace: SubtaskTrace,
        *,
        turn: int = 0,
        detail: str = "",
        source: str = "subtask",
    ) -> None:
        if on_event is None:
            return
        try:
            payload = trace.to_dict()
            metadata = payload.get("metadata") or {}
            parent_message_id = str(payload.get("parent_message_id") or metadata.get("parent_message_id") or "")
            parent_tool_call_id = str(payload.get("parent_tool_call_id") or payload.get("tool_call_id") or metadata.get("parent_tool_call_id") or metadata.get("tool_call_id") or "")
            root_tool_call_id = str(payload.get("root_tool_call_id") or metadata.get("root_tool_call_id") or parent_tool_call_id)
            on_event(
                TaskEvent(
                    kind=TaskEventKind.STEP,
                    turn=int(turn or 0),
                    detail=detail or f"Subtask {trace.title or trace.name} updated.",
                    data={"subtask": payload},
                    source=source,
                    subtask_id=trace.id,
                    parent_message_id=parent_message_id,
                    parent_tool_call_id=parent_tool_call_id,
                    root_tool_call_id=root_tool_call_id,
                )
            )
        except Exception as exc:
            logger.debug("Failed to publish subtask trace event: %s", exc)

    @staticmethod
    def _update_running_subtask_thinking(trace: SubtaskTrace, thinking: str) -> None:
        text = str(thinking or "")
        if not text:
            return
        try:
            for item in reversed(trace.messages):
                if isinstance(item, dict) and item.get("role") == "assistant":
                    item["thinking"] = text
                    return
            pending = Message(role="assistant", content="")
            pending.metadata["subtask_streaming"] = True
            pending.thinking = text
            trace.add_message(pending)
        except Exception as exc:
            logger.debug("Failed to update running subtask thinking: %s", exc)

    @staticmethod
    def _subtask_for_tool_call(conversation: Conversation, tool_call_id: str | None) -> dict[str, Any] | None:
        if not tool_call_id:
            return None
        try:
            for message in reversed(getattr(conversation, "messages", []) or []):
                if getattr(message, "role", "") != "assistant":
                    continue
                for tool_call in getattr(message, "tool_calls", []) or []:
                    if str(tool_call.get("id") or "") != str(tool_call_id):
                        continue
                    result = get_tool_call_result(tool_call)
                    run = result.get("run")
                    if isinstance(run, dict):
                        return dict(run)
        except Exception:
            return None
        return None

    @staticmethod
    def _subtask_display_name(subtask_req: dict[str, Any], mode_slug: str) -> str:
        title = str(subtask_req.get("title") or "").strip()
        if title:
            return title
        capability_id = str(subtask_req.get("capability_id") or "").strip()
        if capability_id:
            return capability_id
        return str(mode_slug or "subtask").strip() or "subtask"

    @staticmethod
    def _subtask_goal(subtask_req: dict[str, Any]) -> str:
        goal = str(subtask_req.get("goal") or "").strip()
        if goal:
            return goal
        message = str(subtask_req.get("message") or "").strip()
        for line in message.splitlines():
            cleaned = line.strip(" -\t")
            if cleaned:
                return cleaned[:240]
        return "Run delegated task"

    def _record_subtask_event(self, trace: SubtaskTrace, event: TaskEvent) -> None:
        if event.kind == TaskEventKind.STEP and isinstance(event.data, Message):
            trace.add_message(event.data)
            return
        if event.kind == TaskEventKind.ERROR:
            trace.error = str(event.data or event.detail or "").strip()

    def _build_switched_policy(
        self,
        *,
        current_policy: RunPolicy,
        conversation: Conversation,
        next_mode: str,
    ) -> RunPolicy:
        from core.config.schema import RetryConfig
        from core.modes.manager import ModeManager
        from core.task.builder import build_run_policy
        from core.config.io import load_app_config

        mode_manager = ModeManager(getattr(conversation, "work_dir", None) or None)
        retry_cfg = RetryConfig(
            max_retries=int(getattr(current_policy.retry, "max_retries", 3) or 3),
            base_delay=float(getattr(current_policy.retry, "base_delay", 1.0) or 1.0),
            backoff_factor=float(getattr(current_policy.retry, "backoff_factor", 2.0) or 2.0),
        )
        tool_permissions = None
        try:
            tool_permissions = load_app_config().permissions
        except Exception:
            pass
        next_policy = build_run_policy(
            mode_slug=str(next_mode or "chat") or "chat",
            mode_manager=mode_manager,
            retry_config=retry_cfg,
            tool_permissions=tool_permissions,
        )
        return replace(
            next_policy,
            model=current_policy.model,
            temperature=current_policy.temperature,
            max_tokens=current_policy.max_tokens,
        )

    async def _run_subtask(
        self,
        *,
        subtask_req: dict,
        provider: Provider,
        conversation: Conversation,
        approval_callback,
        questions_callback,
        cancel_event,
        on_event=None,
        turn: int = 0,
    ) -> SubTaskOutcome:
        """Spawn a child Task in an independent conversation context."""
        mode_slug = subtask_req.get("mode", "agent")
        message = subtask_req.get("message", "")
        trace = SubtaskTrace(
            id=str(subtask_req.get("id") or f"subtask-{uuid.uuid4().hex[:12]}"),
            kind=str(subtask_req.get("kind") or "subagent"),
            name=self._subtask_display_name(subtask_req, mode_slug),
            title=self._subtask_display_name(subtask_req, mode_slug),
            goal=self._subtask_goal(subtask_req),
            mode=str(mode_slug or "agent"),
            depth=int(subtask_req.get("depth") or 0),
            metadata={
                "capability_id": str(subtask_req.get("capability_id") or ""),
                "capabilities": list(subtask_req.get("capabilities") or []),
                "tool_call_id": str(subtask_req.get("tool_call_id") or ""),
                "parent_message_id": str(subtask_req.get("parent_message_id") or ""),
                "parent_tool_call_id": str(subtask_req.get("parent_tool_call_id") or subtask_req.get("tool_call_id") or ""),
                "root_tool_call_id": str(subtask_req.get("root_tool_call_id") or subtask_req.get("tool_call_id") or ""),
            },
        )

        try:
            from core.task.builder import build_run_policy
            from core.config.io import load_app_config
            from models.provider import provider_matches_name, split_model_ref

            tool_permissions = None
            try:
                tool_permissions = load_app_config().permissions
            except Exception:
                pass
            tool_selection = ToolSelectionPolicy.from_dict(subtask_req.get("tool_selection"))
            child_policy = build_run_policy(
                mode_slug=mode_slug,
                tool_selection=tool_selection,
                tool_permissions=tool_permissions,
            )
            updates: dict[str, Any] = {"source": "sub_task"}
            model_ref = str(subtask_req.get("model_ref") or "").strip()
            if model_ref:
                provider_name, model_name = split_model_ref(model_ref)
                if provider_name and not provider_matches_name(provider, provider_name):
                    logger.debug(
                        "Ignoring subtask provider override '%s' because active provider is '%s'",
                        provider_name,
                        getattr(provider, "name", ""),
                    )
                if model_name:
                    updates["model"] = model_name
            try:
                max_turns = int(subtask_req.get("max_turns") or 0)
                if max_turns > 0:
                    updates["max_turns"] = max_turns
            except Exception:
                pass
            if subtask_req.get("auto_spillover"):
                updates["auto_compress_enabled"] = False
            if updates:
                child_policy = replace(child_policy, **updates)

            child_conv = Conversation(
                title=f"Sub-task: {mode_slug}",
                messages=[Message(role="user", content=message)],
                mode=mode_slug,
            )
            runtime_instructions = str(
                subtask_req.get("instructions")
                or subtask_req.get("system_prompt_override")
                or ""
            ).strip()
            if runtime_instructions:
                try:
                    child_cfg = child_conv.get_llm_config().with_updates(
                        system_prompt_override=runtime_instructions
                    )
                    child_conv.set_llm_config(child_cfg)
                except Exception as e:
                    logger.debug("Failed to apply subtask runtime instructions: %s", e)
            # Inherit work_dir from parent
            try:
                child_conv.work_dir = getattr(conversation, "work_dir", ".") or "."
            except Exception as e:
                logger.debug("Failed to inherit work_dir for subtask: %s", e)
            try:
                trace.add_message(child_conv.messages[0])
            except Exception as exc:
                logger.debug("Failed to record subtask prompt: %s", exc)
            self._publish_subtask_trace_event(
                on_event,
                trace,
                turn=turn,
                detail=f"Subtask {trace.title} prompt recorded.",
            )
            child_task = Task(client=self._client, tool_manager=self._tool_manager)
            pending_thinking: list[str] = []
            last_publish_at = 0.0

            def publish_trace(detail: str = "Subtask updated.", *, force: bool = False) -> None:
                nonlocal last_publish_at
                now = time.monotonic()
                if not force and (now - last_publish_at) < 1.0:
                    return
                last_publish_at = now
                self._publish_subtask_trace_event(on_event, trace, turn=turn, detail=detail)

            def child_on_event(event: TaskEvent) -> None:
                try:
                    event.source = "subtask"
                    event.subtask_id = trace.id
                    self._record_subtask_event(trace, event)
                    publish_trace(str(getattr(event, "detail", "") or "Subtask updated."))
                except Exception as exc:
                    logger.debug("Failed to record subtask event: %s", exc)

            def child_on_thinking(thinking: str) -> None:
                text = str(thinking or "")
                if text:
                    pending_thinking.append(text)
                    self._update_running_subtask_thinking(trace, "".join(pending_thinking))
                    publish_trace("Subtask thinking updated.")

            result = await child_task.run(
                provider=provider,
                conversation=child_conv,
                policy=child_policy,
                on_event=child_on_event,
                on_token=None,
                on_thinking=child_on_thinking,
                approval_callback=approval_callback,
                questions_callback=questions_callback,
                cancel_event=cancel_event,
            )

            combined_thinking = "".join(pending_thinking).strip()
            if combined_thinking:
                for item in reversed(trace.messages):
                    if isinstance(item, dict) and item.get("role") == "assistant":
                        if not str(item.get("thinking") or "").strip():
                            item["thinking"] = combined_thinking
                        break

            if result.status == TaskStatus.COMPLETED and result.final_message:
                if combined_thinking and not str(getattr(result.final_message, "thinking", "") or "").strip():
                    result.final_message.thinking = combined_thinking
                metadata = getattr(result.final_message, "metadata", {}) or {}
                max_turns_failure = self._is_max_turns_failure_message(
                    result.final_message.content or "",
                    metadata,
                )
                if not any(item.get("id") == getattr(result.final_message, "id", "") for item in trace.messages if isinstance(item, dict)):
                    trace.add_message(result.final_message)
                status = TaskStatus.FAILED if max_turns_failure else result.status
                trace.finish(
                    SubtaskTraceStatus.FAILED if max_turns_failure else SubtaskTraceStatus.COMPLETED,
                    final_message=result.final_message.content or "Sub-task completed (no output).",
                    error="Sub-task reached max turns before completion." if max_turns_failure else "",
                )
                publish_trace("Subtask completed.", force=True)
                return SubTaskOutcome(
                    status=status,
                    message=result.final_message.content or "Sub-task completed (no output).",
                    completion_command=str(metadata.get("completion_command") or "").strip(),
                    completed=bool(metadata.get("completion")) and not max_turns_failure,
                    trace=trace,
                )
            if result.status == TaskStatus.FAILED:
                trace.finish(SubtaskTraceStatus.FAILED, error=str(result.error or ""))
                publish_trace("Subtask failed.", force=True)
                return SubTaskOutcome(
                    status=result.status,
                    message=f"Sub-task failed: {result.error}",
                    trace=trace,
                )
            if result.status == TaskStatus.CANCELLED:
                trace.finish(SubtaskTraceStatus.CANCELLED, final_message="Sub-task was cancelled.")
                publish_trace("Subtask cancelled.", force=True)
                return SubTaskOutcome(
                    status=result.status,
                    message="Sub-task was cancelled.",
                    trace=trace,
                )
            trace.finish(SubtaskTraceStatus.COMPLETED, final_message="Sub-task completed.")
            publish_trace("Subtask completed.", force=True)
            return SubTaskOutcome(
                status=result.status,
                message="Sub-task completed.",
                completed=result.status == TaskStatus.COMPLETED,
                trace=trace,
            )
        except Exception as e:
            logger.error("Sub-task failed: %s", e)
            trace.finish(SubtaskTraceStatus.FAILED, error=str(e))
            self._publish_subtask_trace_event(on_event, trace, turn=turn, detail="Subtask error.")
            return SubTaskOutcome(
                status=TaskStatus.FAILED,
                message=f"Sub-task error: {e}",
                trace=trace,
            )

    # ------------------------------------------------------------------
    # Context management (condense)
    # ------------------------------------------------------------------

    async def _maybe_condense(
        self,
        conversation: Conversation,
        provider: Provider,
        policy: RunPolicy,
    ) -> None:
        """使用统一的 ContextManager 进行上下文压缩。"""
        try:
            cfg = load_app_config()
        except Exception as e:
            logger.debug("Failed to load app config, using defaults: %s", e)
            cfg = AppConfig()

        try:
            cfg_enabled = bool(getattr(getattr(cfg, "context", None), "agent_auto_compress_enabled", True))
        except Exception as e:
            logger.debug("Failed to read auto_compress_enabled config: %s", e)
            cfg_enabled = True

        if policy.auto_compress_enabled is False:
            return
        if not cfg_enabled:
            return

        from core.context.condenser import ContextCondenser, CondensePolicy
        from core.context.manager import ContextManager

        condenser = ContextCondenser(self._client)
        context_manager = ContextManager(
            condenser=condenser,
            policy=CondensePolicy(
                max_active_messages=20,
                token_threshold_ratio=0.7,
                keep_last_n=3,
            )
        )

        should_compress = context_manager._should_compress(
            conversation,
            int(policy.context_window_limit)
        )

        if should_compress:
            logger.info("触发上下文压缩")
            await condenser.auto_condense(
                conversation=conversation,
                provider=provider,
                context_window_limit=int(policy.context_window_limit),
                app_config=cfg,
                policy=context_manager.policy,
            )

    async def _force_condense(
        self,
        conversation: Conversation,
        provider: Provider,
        policy: RunPolicy,
    ) -> None:
        """Emergency condense on context overflow."""
        from core.context.condenser import ContextCondenser as Condenser
        condenser = Condenser(self._client)
        state = conversation.get_state()
        await condenser.condense_state(conversation, provider, state, keep_last_n=3)
        conversation.set_state(state)
        logger.info("Emergency condense complete")

    def _attach_state_snapshot(self, conversation: Conversation, msg: Message) -> None:
        """Attach state snapshot to message."""
        self._tool_executor.attach_state_snapshot(conversation, msg)
