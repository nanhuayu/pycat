"""Tool-call coordination for one assistant turn."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from models.conversation import Conversation, Message
from models.provider import Provider

from core.task.control_messages import FINALIZE_TEXT, MODE_SWITCH_MESSAGE, REPETITION_WARNING
from core.task.event_emitter import EventEmitter
from core.task.repetition import ToolRepetitionDetector
from core.task.types import RunPolicy, TaskEventKind, TaskTurnState, TurnContext, TurnOutcome, TurnOutcomeKind
from core.tools.base import ToolResult

logger = logging.getLogger(__name__)


@dataclass
class ToolCallCoordinator:
    """Executes assistant tool calls and converts results back into turn outcomes."""

    client: Any
    tool_executor: Any
    subtask_coordinator: Any
    tool_result_to_string: Callable[[ToolResult | str], str]
    summarize_tool_result: Callable[[str, str], str]
    build_tool_result_block: Callable[..., Awaitable[dict[str, Any]]]
    build_completion_message: Callable[..., Message]
    build_switched_policy: Callable[..., RunPolicy]
    should_finalize_next_turn: Callable[..., bool]

    async def execute(
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
        turns_limit: int,
    ) -> TurnOutcome:
        turn_context.had_tool_work = True
        total_tools = len(assistant_msg.tool_calls or [])
        for tool_call in assistant_msg.tool_calls or []:
            if cancel_event and cancel_event.is_set():
                turn_context.state = TaskTurnState.CANCELLED
                return TurnOutcome(kind=TurnOutcomeKind.CANCELLED, context=turn_context, final_message=assistant_msg)

            tool_name, args, tool_call_id = self.tool_executor.parse_tool_call(tool_call)
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
                turn_context.runtime_messages = [Message(role="user", content=REPETITION_WARNING)]
                repetition_detector.reset()
                turn_context.state = TaskTurnState.TURN_COMPLETE
                return TurnOutcome(kind=TurnOutcomeKind.CONTINUE, context=turn_context, final_message=assistant_msg)

            allowed = self.tool_executor.is_tool_allowed(tool_name, policy)
            emitter.emit(
                TaskEventKind.TOOL_START,
                turn=turn_context.turn,
                detail=f"Tool {tool_name} started.",
                data={**tool_event_base, "allowed": bool(allowed)},
            )
            context = self.tool_executor.build_tool_context(
                conversation=conversation,
                provider=provider,
                approval_callback=approval_callback,
                questions_callback=questions_callback,
                llm_client=self.client,
                policy=policy,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
            )
            tool_result = await self.tool_executor.execute_tool(
                tool_name=tool_name,
                tool_args=args,
                allowed=allowed,
                policy=policy,
                context=context,
            )
            result_text = self.tool_result_to_string(tool_result)
            tool_is_error = bool(getattr(tool_result, "is_error", False))

            subtask_execution = await self.subtask_coordinator.handle_control_action(
                action=getattr(tool_result, "control_action", None),
                assistant_msg=assistant_msg,
                tool_call_id=tool_call_id,
                conversation=conversation,
                provider=provider,
                policy=policy,
                approval_callback=approval_callback,
                questions_callback=questions_callback,
                cancel_event=cancel_event,
                on_event=on_event,
                turn=turn_context.turn,
            )
            if subtask_execution is not None:
                tool_result = subtask_execution.result
                result_text = self.tool_result_to_string(tool_result)
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
                    "summary": self.summarize_tool_result(tool_name, result_text),
                },
            )

            if (context.state or {}).get("_task_completed"):
                completion_result = str((context.state or {}).get("_completion_result") or result_text or "").strip()
                completion_command = str((context.state or {}).get("_completion_command") or "").strip()
                self.tool_executor.sync_state(conversation, context)
                result_block = await self.build_tool_result_block(
                    conversation=conversation,
                    provider=provider,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    result=tool_result,
                    summary="Completion acknowledged.",
                    tool_args=args if isinstance(args, dict) else {},
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

                final_msg = self.build_completion_message(
                    conversation=conversation,
                    completion_text=completion_result or "Task completed.",
                    completion_command=completion_command,
                )
                emitter.emit(TaskEventKind.STEP, turn=turn_context.turn, data=final_msg)
                emitter.emit(TaskEventKind.COMPLETE, turn=turn_context.turn, data=final_msg)
                conversation.add_message(final_msg)
                turn_context.state = TaskTurnState.TURN_COMPLETE
                return TurnOutcome(kind=TurnOutcomeKind.COMPLETE, context=turn_context, final_message=final_msg)

            self.tool_executor.sync_state(conversation, context)
            result_block = await self.build_tool_result_block(
                conversation=conversation,
                provider=provider,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                result=tool_result,
                tool_args=args if isinstance(args, dict) else {},
            )

            switched_mode = str((context.state or {}).pop("_mode_switch", "") or "").strip().lower()
            next_policy: Optional[RunPolicy] = None
            if switched_mode:
                next_policy = self.build_switched_policy(
                    current_policy=policy,
                    conversation=conversation,
                    next_mode=switched_mode,
                )
                conversation.mode = switched_mode
                try:
                    result_block["result"].setdefault("metadata", {})["mode_switch"] = switched_mode
                    event_metadata = getattr(result_block["event"], "metadata", None)
                    if isinstance(event_metadata, dict):
                        event_metadata["mode_switch"] = switched_mode
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
                turn_context.runtime_messages = [Message(role="user", content=MODE_SWITCH_MESSAGE)]
                turn_context.state = TaskTurnState.TURN_COMPLETE
                return TurnOutcome(
                    kind=TurnOutcomeKind.CONTINUE,
                    context=turn_context,
                    final_message=assistant_msg,
                    next_policy=next_policy,
                )

        turn_context.state = TaskTurnState.TURN_COMPLETE
        if self.should_finalize_next_turn(policy=policy, turn_context=turn_context, turns_limit=turns_limit):
            turn_context.runtime_messages = [Message(role="user", content=FINALIZE_TEXT)]
        else:
            turn_context.runtime_messages = []
        return TurnOutcome(kind=TurnOutcomeKind.CONTINUE, context=turn_context, final_message=assistant_msg)
