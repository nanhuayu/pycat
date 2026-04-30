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

import copy
from dataclasses import replace
import logging
from pathlib import Path
import re
import threading
from typing import Any, Callable, Optional

from models.conversation import Conversation, Message
from models.provider import Provider

from core.llm.client import LLMClient
from core.tools.manager import ToolManager
from core.tools.base import ToolResult
from core.config import load_app_config, AppConfig
from core.state.services.task_service import TaskService
from core.state.services.task_planning_service import TaskPlanningService
from core.task.types import (
    RunPolicy,
    SubTaskOutcome,
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
    "Do not simply describe what you would do — take action."
)

_REPETITION_WARNING = (
    "[WARNING] You have called the same tool with identical arguments multiple times consecutively. "
    "This is not making progress. Please try a different approach, use different arguments, "
    "or call `attempt_completion` if done."
)

_STATE_BOOTSTRAP_MODES = frozenset({"agent", "code", "debug", "plan", "orchestrator"})
_TOOL_RESULT_SPILLOVER_CHARS = 12000
_TOOL_RESULT_PREVIEW_CHARS = 2400


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
        turns_limit = max(1, int(policy.max_turns or 20))
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
        self._bootstrap_session_state(conversation, policy, turn_context.turn)
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
                subtask_outcome = await self._run_subtask(
                    subtask_req=subtask_req,
                    provider=provider,
                    conversation=conversation,
                    on_event=on_event,
                    on_token=on_token,
                    on_thinking=on_thinking,
                    approval_callback=approval_callback,
                    questions_callback=questions_callback,
                    cancel_event=cancel_event,
                )
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
                self._complete_session_tasks(context, completion_result or result_text, conversation)
                self._tool_executor.sync_state(conversation, context)
                tool_msg = self._build_tool_message(
                    conversation=conversation,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    result=tool_result,
                    summary="Completion acknowledged.",
                )
                emitter.emit(TaskEventKind.STEP, turn=turn_context.turn, data=tool_msg)
                conversation.add_message(tool_msg)

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
            tool_msg = self._build_tool_message(
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
                    tool_msg.metadata["mode_switch"] = switched_mode
                except Exception as exc:
                    logger.debug("Failed to annotate tool message with mode switch: %s", exc)

            emitter.emit(TaskEventKind.STEP, turn=turn_context.turn, data=tool_msg)
            conversation.add_message(tool_msg)

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
        turn_context.runtime_messages = []
        return TurnOutcome(kind=TurnOutcomeKind.CONTINUE, context=turn_context, final_message=assistant_msg)

    def _build_tool_message(
        self,
        *,
        conversation: Conversation,
        tool_name: str,
        tool_call_id: Optional[str],
        result: ToolResult | str,
        summary: Optional[str] = None,
    ) -> Message:
        result_text = self._tool_result_to_string(result)
        display_text, spillover = self._maybe_spill_tool_result(
            conversation=conversation,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            result_text=result_text,
        )
        tool_msg = Message(role="tool", content=str(display_text), tool_call_id=tool_call_id)
        try:
            tool_msg.seq_id = conversation.next_seq_id()
        except Exception as e:
            logger.warning("Failed to assign seq_id to tool message: %s", e)

        tool_images = self._extract_tool_images(result)
        if tool_images:
            tool_msg.images = tool_images

        try:
            tool_msg.metadata = tool_msg.metadata or {}
            tool_msg.metadata["name"] = tool_name
            if spillover:
                tool_msg.metadata["tool_result_file"] = spillover.get("path", "")
                tool_msg.metadata["tool_result_chars"] = spillover.get("chars", 0)
                tool_msg.metadata["tool_result_truncated"] = True
        except Exception as e:
            logger.debug("Failed to set tool metadata: %s", e)

        try:
            tool_msg.summary = summary or self._summarize_tool_result(tool_name, display_text)
        except Exception as e:
            logger.debug("Failed to summarize tool result for %s: %s", tool_name, e)

        self._attach_state_snapshot(conversation, tool_msg)
        return tool_msg

    def _maybe_spill_tool_result(
        self,
        *,
        conversation: Conversation,
        tool_name: str,
        tool_call_id: Optional[str],
        result_text: str,
    ) -> tuple[str, dict[str, Any] | None]:
        text = str(result_text or "")
        if len(text) <= _TOOL_RESULT_SPILLOVER_CHARS:
            return text, None

        work_dir = Path(str(getattr(conversation, "work_dir", "") or ".")).resolve()
        target_dir = work_dir / ".pycat" / "tool-results"
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            safe_tool = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(tool_name or "tool")).strip("_") or "tool"
            seq = int(conversation.current_seq_id() or 0) + 1
            call_id = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(tool_call_id or "call")).strip("_")[:40] or "call"
            target_path = (target_dir / f"{seq:06d}_{safe_tool}_{call_id}.txt").resolve()
            if target_dir not in target_path.parents:
                return text[:_TOOL_RESULT_PREVIEW_CHARS], None
            target_path.write_text(text, encoding="utf-8", errors="replace")
            preview = text[:_TOOL_RESULT_PREVIEW_CHARS].rstrip()
            display = (
                f"{preview}\n\n"
                f"... [full tool result stored at {target_path}; {len(text)} chars. "
                "Use read_file if the complete output is needed.]"
            )
            return display, {"path": str(target_path), "chars": len(text)}
        except Exception as exc:
            logger.debug("Failed to spill large tool result for %s: %s", tool_name, exc)
            return text, None

    def _complete_session_tasks(self, context: Any, completion_text: str, conversation: Conversation) -> None:
        try:
            from models.state import SessionState

            state = SessionState.from_dict(
                {
                    k: v for k, v in (getattr(context, "state", {}) or {}).items()
                    if not str(k).startswith("_")
                }
            )
            updated = TaskService.complete_active_tasks(
                state,
                int(conversation.current_seq_id() or 0),
                reason=completion_text,
            )
            if not updated:
                return
            context.state = state.to_dict()
        except Exception as exc:
            logger.debug("Failed to auto-complete session tasks: %s", exc)

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

        if tool_name == "manage_document":
            return lines[0][:220]

        if tool_name == "load_skill":
            for line in lines:
                if line.startswith("Skill:"):
                    return f"Loaded {line.split(':', 1)[1].strip()}."
            return "Skill loaded."

        if tool_name == "read_skill_resource":
            return lines[0][:220]

        if tool_name == "attempt_completion":
            return "Completion acknowledged."

        return lines[0][:220]

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

        mode_manager = ModeManager(getattr(conversation, "work_dir", None) or None)
        retry_cfg = RetryConfig(
            max_retries=int(getattr(current_policy.retry, "max_retries", 3) or 3),
            base_delay=float(getattr(current_policy.retry, "base_delay", 1.0) or 1.0),
            backoff_factor=float(getattr(current_policy.retry, "backoff_factor", 2.0) or 2.0),
        )
        next_policy = build_run_policy(
            mode_slug=str(next_mode or "chat") or "chat",
            mode_manager=mode_manager,
            retry_config=retry_cfg,
        )
        return replace(
            next_policy,
            model=current_policy.model,
            temperature=current_policy.temperature,
            max_tokens=current_policy.max_tokens,
        )

    def _bootstrap_session_state(self, conversation: Conversation, policy: RunPolicy, turn: int) -> None:
        """Ensure agent-like modes start with usable todo/plan/memory state."""
        mode_slug = str(getattr(policy, "mode", "chat") or "chat").strip().lower()
        if mode_slug not in _STATE_BOOTSTRAP_MODES:
            return

        try:
            state = conversation.get_state()
        except Exception:
            return

        changed = False

        try:
            latest_user = ""
            for msg in reversed(getattr(conversation, "messages", []) or []):
                if getattr(msg, "role", "") == "user":
                    latest_user = (getattr(msg, "content", "") or "").strip()
                    if latest_user:
                        break
        except Exception:
            latest_user = ""

        try:
            if not state.get_active_tasks() and latest_user:
                current_seq = int(conversation.current_seq_id() or 0)
                seeded_tasks = TaskPlanningService.build_bootstrap_tasks(
                    request_text=latest_user,
                    mode_slug=mode_slug,
                    current_seq=current_seq,
                )
                if seeded_tasks:
                    state.tasks.extend(seeded_tasks)
                    changed = True
        except Exception as e:
            logger.debug("Failed to seed session task: %s", e)

        try:
            if TaskService.ensure_active_task(state, int(conversation.current_seq_id() or 0)):
                changed = True
        except Exception as e:
            logger.debug("Failed to promote active session task: %s", e)

        try:
            plan_doc = state.ensure_document("plan")
            if not (plan_doc.content or "").strip() and latest_user:
                if mode_slug == "plan":
                    plan_doc.content = (
                        f"Mode: {mode_slug}\n"
                        f"Turn: {turn}\n"
                        "Goal:\n"
                        f"{latest_user.strip()}\n\n"
                        "Discovery:\n"
                        "- Inspect the relevant code paths and documents\n"
                        "- Capture constraints, risks, and unresolved questions\n\n"
                        "Implementation Plan:\n"
                        "1. Gather the missing context\n"
                        "2. Decide architecture and sequencing\n"
                        "3. Present a concrete execution plan"
                    )
                else:
                    plan_doc.content = (
                        f"Mode: {mode_slug}\n"
                        f"Turn: {turn}\n"
                        "Goal:\n"
                        f"{latest_user.strip()}\n\n"
                        "Working Plan:\n"
                        "1. Gather context\n"
                        "2. Execute the next concrete step\n"
                        "3. Update todo/memory as new facts are confirmed"
                    )
                plan_doc.updated_seq = int(conversation.current_seq_id() or 0)
                changed = True
        except Exception as e:
            logger.debug("Failed to seed plan document: %s", e)

        try:
            memory_doc = state.ensure_document("memory")
            if not (memory_doc.content or "").strip():
                memory_doc.content = (
                    "Store confirmed repo facts, important decisions, verified commands, "
                    "or user preferences here when they become relevant."
                )
                memory_doc.updated_seq = int(conversation.current_seq_id() or 0)
                changed = True
        except Exception as e:
            logger.debug("Failed to seed memory document: %s", e)

        try:
            if state.memory.get("active_mode") != mode_slug:
                state.memory["active_mode"] = mode_slug
                changed = True
            work_dir = str(getattr(conversation, "work_dir", "") or "").strip()
            if work_dir and state.memory.get("work_dir") != work_dir:
                state.memory["work_dir"] = work_dir
                changed = True
        except Exception as e:
            logger.debug("Failed to seed structured memory: %s", e)

        if changed:
            try:
                state.last_updated_seq = int(conversation.current_seq_id() or 0)
                conversation.set_state(state)
            except Exception as e:
                logger.debug("Failed to persist bootstrapped session state: %s", e)

    async def _run_subtask(
        self,
        *,
        subtask_req: dict,
        provider: Provider,
        conversation: Conversation,
        on_event,
        on_token,
        on_thinking,
        approval_callback,
        questions_callback,
        cancel_event,
    ) -> SubTaskOutcome:
        """Spawn a child Task in an independent conversation context."""
        mode_slug = subtask_req.get("mode", "code")
        message = subtask_req.get("message", "")

        try:
            from core.task.builder import build_run_policy

            child_policy = build_run_policy(mode_slug=mode_slug)

            child_conv = Conversation(
                title=f"Sub-task: {mode_slug}",
                messages=[Message(role="user", content=message)],
                mode=mode_slug,
            )
            # Inherit work_dir from parent
            try:
                child_conv.work_dir = getattr(conversation, "work_dir", ".") or "."
            except Exception as e:
                logger.debug("Failed to inherit work_dir for subtask: %s", e)
            try:
                child_conv._state_dict = copy.deepcopy(getattr(conversation, "_state_dict", {}) or {})
            except Exception as e:
                logger.debug("Failed to inherit session state for subtask: %s", e)

            child_task = Task(client=self._client, tool_manager=self._tool_manager)
            result = await child_task.run(
                provider=provider,
                conversation=child_conv,
                policy=child_policy,
                on_event=on_event,
                on_token=on_token,
                on_thinking=on_thinking,
                approval_callback=approval_callback,
                questions_callback=questions_callback,
                cancel_event=cancel_event,
            )

            self._merge_subtask_state(parent_conversation=conversation, child_conversation=child_conv)

            if result.status == TaskStatus.COMPLETED and result.final_message:
                metadata = getattr(result.final_message, "metadata", {}) or {}
                return SubTaskOutcome(
                    status=result.status,
                    message=result.final_message.content or "Sub-task completed (no output).",
                    completion_command=str(metadata.get("completion_command") or "").strip(),
                    completed=bool(metadata.get("completion")),
                )
            if result.status == TaskStatus.FAILED:
                return SubTaskOutcome(
                    status=result.status,
                    message=f"Sub-task failed: {result.error}",
                )
            if result.status == TaskStatus.CANCELLED:
                return SubTaskOutcome(
                    status=result.status,
                    message="Sub-task was cancelled.",
                )
            return SubTaskOutcome(
                status=result.status,
                message="Sub-task completed.",
                completed=result.status == TaskStatus.COMPLETED,
            )
        except Exception as e:
            logger.error("Sub-task failed: %s", e)
            return SubTaskOutcome(
                status=TaskStatus.FAILED,
                message=f"Sub-task error: {e}",
            )

    def _merge_subtask_state(self, *, parent_conversation: Conversation, child_conversation: Conversation) -> None:
        try:
            parent_state = parent_conversation.get_state()
            child_state = child_conversation.get_state()
        except Exception as e:
            logger.debug("Failed to load session state for subtask merge: %s", e)
            return

        changed = False

        try:
            for name, child_doc in (child_state.documents or {}).items():
                normalized = str(name or "").strip().lower()
                if normalized not in {"plan", "memory", "report", "notes"}:
                    continue
                child_content = str(getattr(child_doc, "content", "") or "").strip()
                if not child_content:
                    continue
                existing = parent_state.documents.get(name)
                if existing is None or child_content != str(getattr(existing, "content", "") or "").strip():
                    parent_state.documents[name] = copy.deepcopy(child_doc)
                    changed = True
        except Exception as e:
            logger.debug("Failed merging subtask documents: %s", e)

        try:
            for key, value in (child_state.memory or {}).items():
                if key == "active_mode":
                    continue
                if parent_state.memory.get(key) != value:
                    parent_state.memory[key] = value
                    changed = True
        except Exception as e:
            logger.debug("Failed merging subtask memory: %s", e)

        if not changed:
            return

        try:
            parent_state.last_updated_seq = int(parent_conversation.current_seq_id() or 0)
            parent_conversation.set_state(parent_state)
        except Exception as e:
            logger.debug("Failed to persist merged subtask state: %s", e)

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
