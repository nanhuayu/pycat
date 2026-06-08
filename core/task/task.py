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
from typing import Any, Callable, Optional

from models.conversation import Conversation, Message
from models.provider import Provider

from core.llm.client import LLMClient
from core.tools.manager import ToolManager
from core.tools.base import ToolResult
from core.tools.tool_call_archive import ToolCallArchiveService, ToolResultViewService
from core.config import load_app_config, AppConfig
from core.task.types import (
    RunPolicy,
    TaskEvent,
    TaskEventKind,
    TaskResult,
    TaskStopReason,
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
from core.task.agent_loop import build_root_run
from core.task.control_messages import AUTO_CONTINUE_MODES, MAX_NUDGE_COUNT, NUDGE_TEXT
from core.task.stop_policy import TaskStopPolicy
from core.task.subtask import SubtaskCoordinator
from core.task.tool_loop import ToolCallCoordinator
from core.task.trace import (
    trace_for_tool_call,
)

logger = logging.getLogger(__name__)

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
        self._subtask_coordinator = SubtaskCoordinator(self)
        self._pre_turn_hooks: list[Callable] = []
        self._post_turn_hooks: list[Callable] = []
        self._tool_call_archive: ToolCallArchiveService | None = None

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
        run = build_root_run(conversation=conversation, provider=provider, policy=policy)
        provider = run.provider
        conversation = run.conversation
        policy = run.policy
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
                return TaskResult(
                    status=TaskStatus.CANCELLED,
                    final_message=final_assistant,
                    stop_reason=TaskStopReason.CANCELLED,
                )
            if outcome.kind == TurnOutcomeKind.FAILED:
                return TaskResult(
                    status=TaskStatus.FAILED,
                    final_message=final_assistant,
                    error=outcome.error,
                    stop_reason=outcome.stop_reason or TaskStopReason.ERROR,
                )
            return TaskResult(
                status=TaskStatus.COMPLETED,
                final_message=outcome.final_message or final_assistant,
                stop_reason=outcome.stop_reason or TaskStopReason.COMPLETED,
            )

        fallback = self._build_max_turns_message(conversation=conversation, final_assistant=final_assistant)
        if fallback is not None:
            emitter.emit(TaskEventKind.STEP, turn=turns_limit, data=fallback)
            emitter.emit(TaskEventKind.COMPLETE, turn=turns_limit, data=fallback)
            conversation.add_message(fallback)
            return TaskResult(
                status=TaskStatus.FAILED,
                final_message=fallback,
                error="Task loop reached max turns before completion.",
                stop_reason=TaskStopReason.MAX_TURNS,
            )
        return TaskResult(
            status=TaskStatus.FAILED,
            final_message=final_assistant,
            error="Task loop reached max turns before completion.",
            stop_reason=TaskStopReason.MAX_TURNS,
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
                    return TurnOutcome(
                        kind=TurnOutcomeKind.FAILED,
                        context=turn_context,
                        error=str(e2),
                        stop_reason=TaskStopReason.ERROR,
                    )
            turn_context.state = TaskTurnState.FAILED
            return TurnOutcome(
                kind=TurnOutcomeKind.FAILED,
                context=turn_context,
                error=str(e),
                stop_reason=TaskStopReason.ERROR,
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
        mode_slug = (policy.mode or "chat").lower()
        if mode_slug in AUTO_CONTINUE_MODES and turn_context.nudge_count < MAX_NUDGE_COUNT:
            if (
                not bool(getattr(policy, "force_agent_complete", True))
                and str(assistant_msg.content or "").strip()
                and not turn_context.had_tool_work
            ):
                await self._maybe_condense(conversation, provider, policy)
                self._attach_state_snapshot(conversation, assistant_msg)
                turn_context.state = TaskTurnState.TURN_COMPLETE
                return TurnOutcome(kind=TurnOutcomeKind.COMPLETE, context=turn_context, final_message=assistant_msg)
            turn_context.nudge_count += 1
            logger.info(
                "Auto-continue nudge %d/%d (mode=%s)",
                turn_context.nudge_count,
                MAX_NUDGE_COUNT,
                mode_slug,
            )
            turn_context.runtime_messages = [Message(role="user", content=NUDGE_TEXT)]
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
        )

    def _build_tool_call_coordinator(self) -> ToolCallCoordinator:
        return ToolCallCoordinator(
            client=self._client,
            tool_executor=self._tool_executor,
            subtask_coordinator=self._subtask_coordinator,
            tool_result_to_string=self._tool_result_to_string,
            summarize_tool_result=self._summarize_tool_result,
            build_tool_result_block=self._build_tool_result_block_async,
            build_completion_message=self._build_completion_message,
            build_switched_policy=self._build_switched_policy,
            should_finalize_next_turn=self._should_finalize_next_turn,
        )

    def _should_finalize_next_turn(
        self,
        *,
        policy: RunPolicy,
        turn_context: TurnContext,
        turns_limit: int,
    ) -> bool:
        return TaskStopPolicy.should_finalize_next_turn(
            policy=policy,
            turn_context=turn_context,
            turns_limit=turns_limit,
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
        provider: Provider | None = None,
        tool_name: str,
        tool_call_id: Optional[str],
        result: ToolResult | str,
        summary: Optional[str] = None,
        tool_args: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        result_text = self._tool_result_to_string(result)
        raw_for_archive = result.content if isinstance(result, ToolResult) else result

        # Lazy-init archive service per conversation session. Tool outputs belong to
        # the same workspace session as artifacts/state instead of the process cwd.
        archive_work_dir = str(getattr(conversation, "work_dir", "") or ".")
        archive_session_id = str(getattr(conversation, "id", "") or "default")
        archive_key = (str(archive_work_dir), str(archive_session_id))
        current_key = getattr(self._tool_call_archive, "_pycat_key", None)
        if self._tool_call_archive is None or current_key != archive_key:
            work_dir = str(getattr(conversation, "work_dir", "") or ".")
            self._tool_call_archive = ToolCallArchiveService(work_dir, conversation_id=getattr(conversation, "id", None))
            try:
                setattr(self._tool_call_archive, "_pycat_key", archive_key)
            except Exception:
                pass

        handle = self._tool_call_archive.process(
            tool_name=tool_name,
            raw_text=raw_for_archive,
            tool_call_id=tool_call_id,
            tool_args=tool_args or {},
            seq_id=int(conversation.current_seq_id() or 0) + 1,
        )
        if handle.archive is not None:
            archived_text = result_text
            try:
                archived_text = self._tool_call_archive.archive_store.read_original(handle.archive)
            except Exception:
                archived_text = result_text
            view_service = ToolResultViewService(
                work_dir=archive_work_dir,
                conversation_id=getattr(conversation, "id", None),
                client=self._client,
                provider=provider,
                conversation=conversation,
            )
            handle = await view_service.build_display(
                tool_name=tool_name,
                text=archived_text,
                archive_result=handle,
            )

        tool_images = self._extract_tool_images(result)
        metadata: dict[str, Any] = {"name": tool_name}

        try:
            metadata.update(handle.to_metadata())
            subtask = trace_for_tool_call(conversation, tool_call_id)
            if subtask and str(tool_name or '') == "agent__run":
                metadata["subtask_status"] = str(subtask.get("status") or "")
                metadata["subtask_summary"] = str(subtask.get("final_message") or subtask.get("error") or subtask.get("goal") or "")[:220]
        except Exception as e:
            logger.debug("Failed to set tool metadata: %s", e)

        try:
            if summary:
                result_summary = summary
            elif handle.summary:
                result_summary = handle.summary
            elif handle.strategy == "inline":
                result_summary = self._summarize_tool_result(tool_name, handle.display)
            else:
                result_summary = ""
        except Exception as e:
            logger.debug("Failed to summarize tool result for %s: %s", tool_name, e)
            result_summary = summary or ""

        state_snapshot: dict[str, Any] | None = None
        try:
            state = conversation.get_state()
            if handle.archive is not None:
                state.remember_archive(handle.archive)
            self._record_work_trace_step(
                state=state,
                conversation=conversation,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                result_summary=result_summary,
                metadata=metadata,
                handle=handle,
            )
            conversation.set_state(state)
            state_snapshot = state.to_dict()
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

        event_message = Message(
            role="tool",
            content=handle.display,
            tool_call_id=str(tool_call_id or ""),
            images=list(tool_images),
            metadata={
                "role": "tool_result",
                "tool_name": tool_name,
                "name": tool_name,
                "summary": result_summary,
                "result": result_payload,
                **dict(metadata),
            },
            state_snapshot=state_snapshot,
        )
        event_message.summary = result_summary

        return {
            "tool_call_id": str(tool_call_id or ""),
            "tool_name": tool_name,
            "result": result_payload,
            "images": list(tool_images),
            "state_snapshot": state_snapshot,
            "event": event_message,
        }

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
        label = Task._work_trace_label(tool_name)
        target = Task._work_trace_target(tool_name, metadata)
        refs: list[str] = []
        content_id = ""
        chars = 0
        try:
            archive = getattr(handle, "archive", None)
            if archive is not None:
                content_id = str(getattr(archive, "id", "") or "")
                chars = int(getattr(archive, "size", 0) or 0)
                archive_ref = str(getattr(archive, "original_ref", "") or "")
                if archive_ref:
                    refs.append(archive_ref)
                metadata = getattr(archive, "metadata", {}) or {}
                refs.extend([str(item) for item in (metadata.get("references") or []) if str(item).strip()])
        except Exception:
            pass
        for key in ("content_id", "archive_ref", "subtask_summary"):
            value = str(metadata.get(key) or "").strip()
            if value and value not in refs:
                refs.append(value)
        try:
            state.record_work_step(
                seq=int(conversation.current_seq_id() or 0),
                turn=0,
                kind=label,
                label=label,
                tool_name=str(tool_name or ""),
                target=target,
                status="completed",
                summary=str(result_summary or "")[:500],
                refs=refs[:8],
                content_id=content_id,
                chars=chars,
                goal=Task._latest_user_goal(conversation),
            )
        except Exception as exc:
            logger.debug("Failed to record work trace for %s/%s: %s", tool_name, tool_call_id, exc)

    @staticmethod
    def _work_trace_label(tool_name: str) -> str:
        name = str(tool_name or "").strip()
        if name.startswith("file__"):
            return {
                "file__read": "read",
                "file__list": "inspect",
                "file__search": "inspect",
                "file__write": "edit",
                "file__edit": "edit",
                "file__patch": "edit",
                "file__delete": "edit",
            }.get(name, "file")
        if name.startswith("web__"):
            return "search" if name == "web__search" else "read"
        if name.startswith("shell__") or name.startswith("python__"):
            return "execute"
        if name.startswith("state__artifact"):
            return "write_artifact"
        if name.startswith("state__todo"):
            return "update_todo"
        if name.startswith("state__memory"):
            return "update_memory"
        if name == "agent__complete":
            return "complete"
        if name == "agent__run" or name.startswith("agent__"):
            return "delegate"
        if name.startswith("capability__"):
            return "summarize"
        return "tool"

    @staticmethod
    def _work_trace_target(tool_name: str, metadata: dict[str, Any]) -> str:
        if str(tool_name or "") == "agent__run":
            return str(metadata.get("subtask_summary") or metadata.get("subtask_status") or "").strip()[:160]
        for key in ("content_id", "archive_ref", "mode_switch"):
            value = str(metadata.get(key) or "").strip()
            if value:
                return value[:160]
        return str(tool_name or "").strip()

    @staticmethod
    def _latest_user_goal(conversation: Conversation) -> str:
        for msg in reversed(getattr(conversation, "messages", []) or []):
            if getattr(msg, "role", "") == "user" and str(getattr(msg, "content", "") or "").strip():
                return str(msg.content or "").strip()
        return ""

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

        if tool_name in {"state__todo", "state__artifact", "state__memory"}:
            return lines[0][:220]

        if tool_name == "skill__load":
            for line in lines:
                if line.startswith("Skill:"):
                    return f"Loaded {line.split(':', 1)[1].strip()}."
            return "Skill loaded."

        if tool_name == "skill__read_resource":
            return lines[0][:220]

        if tool_name == "agent__complete":
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
        from core.config.io import load_app_config
        from core.runtime.policy_factory import RuntimePolicyFactory

        mode_manager = ModeManager(getattr(conversation, "work_dir", None) or None)
        retry_cfg = RetryConfig(
            max_retries=int(getattr(current_policy.retry, "max_retries", 3) or 3),
            base_delay=float(getattr(current_policy.retry, "base_delay", 1.0) or 1.0),
            backoff_factor=float(getattr(current_policy.retry, "backoff_factor", 2.0) or 2.0),
        )
        app_settings = {}
        try:
            app_settings = load_app_config().to_dict()
        except Exception:
            pass
        next_policy = RuntimePolicyFactory.build(
            conversation=conversation,
            app_settings=app_settings,
            mode_slug=str(next_mode or "chat") or "chat",
            mode_manager=mode_manager,
            retry_config=retry_cfg,
            tool_permissions=current_policy.tool_permissions,
            source=current_policy.source,
        )
        return replace(
            next_policy,
            model=current_policy.model,
            temperature=current_policy.temperature,
            max_tokens=current_policy.max_tokens,
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
        """Run the unified context maintenance path."""
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

        if policy.auto_compress_enabled is False or not cfg_enabled:
            return

        from core.context.maintenance import ContextMaintenanceService, MaintenancePolicy

        await ContextMaintenanceService(
            MaintenancePolicy(
                keep_last_turns=3,
                soft_turns=5,
                token_soft_ratio=0.7,
                token_hard_ratio=0.7,
            )
        ).maintain_async(
            conversation,
            client=self._client,
            provider=provider,
            context_window_limit=int(policy.context_window_limit),
            current_seq=conversation.current_seq_id(),
        )

    async def _force_condense(
        self,
        conversation: Conversation,
        provider: Provider,
        policy: RunPolicy,
    ) -> None:
        """Emergency condense on context overflow."""
        from core.context.maintenance import ContextMaintenanceService, MaintenancePolicy

        await ContextMaintenanceService(
            MaintenancePolicy(keep_last_turns=3)
        ).maintain_async(
            conversation,
            client=self._client,
            provider=provider,
            context_window_limit=int(policy.context_window_limit),
            current_seq=conversation.current_seq_id(),
            force=True,
        )
        logger.info("Emergency condense complete")

    def _attach_state_snapshot(self, conversation: Conversation, msg: Message) -> None:
        """Attach state snapshot to message."""
        self._tool_executor.attach_state_snapshot(conversation, msg)
