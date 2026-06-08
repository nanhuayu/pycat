from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Callable

from core.task.agent_loop import build_child_run, run_child_agent
from core.task.agent_run import AgentRunResult
from core.task.trace import attach_trace_to_tool_call, build_trace, publish_trace_event
from core.task.types import RunPolicy, SubtaskTraceStatus, TaskEvent, TaskResult, TaskStatus, TaskStopReason
from core.tools.base import ToolControlAction, ToolResult
from models.conversation import Conversation, Message
from models.provider import Provider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubtaskExecution:
    result: ToolResult
    agent_result: AgentRunResult | None = None


class SubtaskCoordinator:
    """Runs typed child-agent control actions emitted by tools."""

    def __init__(self, task) -> None:
        self._task = task

    async def handle_control_action(
        self,
        *,
        action: ToolControlAction | None,
        assistant_msg: Message,
        tool_call_id: str | None,
        conversation: Conversation,
        provider: Provider,
        policy: RunPolicy,
        approval_callback,
        questions_callback,
        cancel_event,
        on_event: Callable[[TaskEvent], None] | None,
        turn: int,
    ) -> SubtaskExecution | None:
        if action is None or action.kind != "schedule_subtask" or action.subtask is None:
            return None

        payload = action.subtask.to_dict()
        if not payload.get("id"):
            payload["id"] = f"subtask-{uuid.uuid4().hex[:12]}"
        if tool_call_id:
            payload["tool_call_id"] = tool_call_id
            payload["parent_message_id"] = str(getattr(assistant_msg, "id", "") or "")
            payload["parent_tool_call_id"] = tool_call_id
            payload["root_tool_call_id"] = tool_call_id
            preview_trace = build_trace(payload)
            attach_trace_to_tool_call(assistant_msg, tool_call_id, preview_trace)
            publish_trace_event(
                on_event,
                preview_trace,
                turn=turn,
                detail=f"Agent run {preview_trace.title} started.",
            )

        child_run = build_child_run(
            payload=payload,
            parent_conversation=conversation,
            provider=provider,
            base_policy=policy,
        )
        try:
            child_result = await run_child_agent(
                task=self._task,
                run=child_run,
                approval_callback=approval_callback,
                questions_callback=questions_callback,
                cancel_event=cancel_event,
                on_event=on_event,
                turn=turn,
            )
        except Exception as exc:
            logger.error("Nested agent run failed: %s", exc)
            trace = child_run.trace or build_trace(payload)
            trace.finish(SubtaskTraceStatus.FAILED, error=str(exc))
            publish_trace_event(on_event, trace, turn=turn, detail="Agent run error.")
            child_result = AgentRunResult(
                context=child_run,
                task_result=TaskResult(
                    status=TaskStatus.FAILED,
                    error=str(exc),
                    stop_reason=TaskStopReason.ERROR,
                ),
            )

        if tool_call_id:
            attach_trace_to_tool_call(assistant_msg, tool_call_id, child_result.context.trace)

        child_trace = child_result.context.trace
        child_message = ""
        if child_trace is not None:
            child_message = str(child_trace.final_message or child_trace.error or child_trace.goal or "").strip()
        message = child_message or child_result.message or "Agent run completed."
        is_error = getattr(child_result.task_result, "status", TaskStatus.COMPLETED) == TaskStatus.FAILED
        return SubtaskExecution(result=ToolResult(message, is_error=is_error), agent_result=child_result)
