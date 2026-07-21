"""Trace helpers for nested agent runs."""
from __future__ import annotations

import logging
from typing import Any

from models.conversation import Message, get_tool_call_result, set_tool_call_result
from models.contracts.agent import SubtaskTrace, RunEvent, RunEventKind

logger = logging.getLogger(__name__)


def run_display_name(payload: dict[str, Any], mode_slug: str = "agent") -> str:
    title = str(payload.get("title") or "").strip()
    if title:
        return title
    capability_id = str(payload.get("capability_id") or "").strip()
    if capability_id:
        return capability_id
    return str(mode_slug or "subtask").strip() or "subtask"


def run_goal(payload: dict[str, Any]) -> str:
    goal = str(payload.get("goal") or "").strip()
    if goal:
        return goal
    message = str(payload.get("message") or "").strip()
    for line in message.splitlines():
        cleaned = line.strip(" -\t")
        if cleaned:
            return cleaned[:240]
    return "Run delegated task"


def build_trace(payload: dict[str, Any]) -> SubtaskTrace:
    mode_slug = str(payload.get("mode") or "agent")
    return SubtaskTrace(
        id=str(payload.get("id") or ""),
        kind=str(payload.get("kind") or "subagent"),
        name=run_display_name(payload, mode_slug),
        title=run_display_name(payload, mode_slug),
        goal=run_goal(payload),
        mode=mode_slug,
        depth=int(payload.get("depth") or 0),
        metadata={
            "capability_id": str(payload.get("capability_id") or ""),
            "capabilities": list(payload.get("capabilities") or []),
            "agent_id": str(payload.get("agent_id") or mode_slug or ""),
            "mode": mode_slug,
            "tool_call_id": str(payload.get("tool_call_id") or ""),
            "parent_message_id": str(payload.get("parent_message_id") or ""),
            "parent_tool_call_id": str(payload.get("parent_tool_call_id") or payload.get("tool_call_id") or ""),
            "root_tool_call_id": str(payload.get("root_tool_call_id") or payload.get("tool_call_id") or ""),
            "child_session_id": str(payload.get("child_session_id") or ""),
            "parent_session_id": str(payload.get("parent_session_id") or ""),
            "context_refs": list(payload.get("context_refs") or []),
            "shared_context_policy": str(payload.get("shared_context_policy") or ""),
            "effective_max_turns": payload.get("effective_max_turns"),
            "profile_max_turns": payload.get("profile_max_turns"),
        },
    )


def attach_trace_to_tool_call(assistant_msg: Message, tool_call_id: str | None, trace: SubtaskTrace | None) -> None:
    if trace is None:
        return
    try:
        payload = trace.to_dict()
        if tool_call_id:
            payload["tool_call_id"] = tool_call_id
            payload["parent_tool_call_id"] = tool_call_id
            payload["root_tool_call_id"] = tool_call_id
        parent_message_id = str(getattr(assistant_msg, "id", "") or "")
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
                metadata.update(
                    {
                        key: value
                        for key, value in metadata_payload.items()
                        if value not in (None, "", [])
                    }
                )
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
        logger.debug("Failed to attach nested agent trace: %s", exc)


def publish_trace_event(
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
            RunEvent(
                kind=RunEventKind.STEP,
                turn=int(turn or 0),
                detail=detail or f"Agent run {trace.title or trace.name} updated.",
                data={"subtask": payload},
                source=source,
                subtask_id=trace.id,
                parent_message_id=parent_message_id,
                parent_tool_call_id=parent_tool_call_id,
                root_tool_call_id=root_tool_call_id,
            )
        )
    except Exception as exc:
        logger.debug("Failed to publish nested agent trace event: %s", exc)


def record_trace_event(trace: SubtaskTrace, event: RunEvent) -> None:
    if event.kind == RunEventKind.STEP and isinstance(event.data, Message):
        trace.add_message(event.data)
        return
    if event.kind == RunEventKind.TOOL_END and isinstance(event.data, dict):
        _attach_tool_end_to_trace(trace, event)
        return
    if event.kind == RunEventKind.ERROR:
        trace.error = str(event.data or event.detail or "").strip()


def _attach_tool_end_to_trace(trace: SubtaskTrace, event: RunEvent) -> None:
    data = dict(event.data or {})
    tool_call_id = str(data.get("tool_call_id") or "").strip()
    if not tool_call_id:
        return
    summary = str(data.get("summary") or event.detail or "").strip()
    is_error = bool(data.get("is_error"))
    for item in reversed(trace.messages):
        if not isinstance(item, dict) or item.get("role") != "assistant":
            continue
        for tool_call in item.get("tool_calls") or []:
            if not isinstance(tool_call, dict) or str(tool_call.get("id") or "") != tool_call_id:
                continue
            existing = tool_call.get("result")
            if isinstance(existing, dict) and (existing.get("content") or existing.get("summary")):
                return
            result_payload = {
                "type": "tool_result",
                "content": summary or ("Tool failed." if is_error else "Tool completed."),
                "summary": (summary or ("Tool failed." if is_error else "Tool completed."))[:220],
                "metadata": {
                    "name": str(data.get("tool_name") or ""),
                    "tool_call_id": tool_call_id,
                    "is_error": is_error,
                    "phase": str(data.get("phase") or "end"),
                },
            }
            set_tool_call_result(tool_call, result_payload)
            return


def update_running_thinking(trace: SubtaskTrace, thinking: str) -> None:
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
        logger.debug("Failed to update nested agent thinking: %s", exc)


def trace_for_tool_call(conversation, tool_call_id: str | None) -> dict[str, Any] | None:
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
