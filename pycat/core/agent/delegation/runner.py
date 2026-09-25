from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Callable

from pycat.core.agent.delegation.subagent import build_child_run, run_child_agent
from pycat.core.agent.events.trace import attach_trace_to_tool_call, build_trace, publish_trace_event
from pycat.core.agent.run.state import AgentRunResult
from pycat.core.observability.debug_trace import DebugTraceContext
from pycat.core.state.artifact import ArtifactService
from pycat.core.tools.base import ToolControlAction, ToolResult
from pycat.models.contracts.agent import RunEvent, RunPolicy, RunResult, RunStatus, RunStopReason, SubtaskTraceStatus
from pycat.models.conversation import Conversation, Message
from pycat.models.provider import Provider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubagentExecution:
    result: ToolResult
    agent_result: AgentRunResult | None = None


class SubagentRunner:
    """Runs typed child-agent control actions emitted by tools."""

    def __init__(
        self,
        task,
        *,
        provider_catalog_provider: Callable[[], Iterable[Provider]] | None = None,
    ) -> None:
        self._task = task
        self._provider_catalog_provider = provider_catalog_provider or (lambda: ())

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
        on_event: Callable[[RunEvent], None] | None,
        turn: int,
        debug_trace: DebugTraceContext | None = None,
    ) -> SubagentExecution | None:
        if action is None or action.kind != "schedule_subtask" or action.subtask is None:
            return None

        payload = action.subtask.to_dict()
        if not payload.get("id"):
            payload["id"] = f"subtask-{uuid.uuid4().hex[:12]}"
        subtask_id = str(payload.get("id") or "")
        subtask_trace: DebugTraceContext | None = None
        subtask_node_id = ""
        if debug_trace is not None:
            subtask_node_id = debug_trace.sink.subtask_node_id(subtask_id)
            subtask_trace = debug_trace.child(
                node_id=subtask_node_id,
                parent_id=debug_trace.node_id or debug_trace.parent_id,
                subtask_id=subtask_id,
                tool_call_id=tool_call_id or "",
                scope_id=subtask_id,
                default_purpose="subtask",
            )
            subtask_trace.record_event(
                kind="subtask",
                phase="start",
                turn=turn,
                node_id=subtask_node_id,
                parent_id=subtask_trace.parent_id,
                name=str(payload.get("mode") or "agent"),
                status="running",
                tool_call_id=tool_call_id or "",
                subtask_id=subtask_id,
                summary=str(payload.get("task") or payload.get("goal") or payload.get("message") or "")[:220],
                data={
                    "mode": str(payload.get("mode") or ""),
                    "child_session_id": str(payload.get("child_session_id") or ""),
                },
            )
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
            app_settings=getattr(self._task, "_app_settings", {}) or {},
            providers=self._provider_catalog_provider(),
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
                debug_trace=subtask_trace,
            )
        except Exception as exc:
            logger.error("Nested agent run failed: %s", exc)
            trace = child_run.trace or build_trace(payload)
            trace.finish(SubtaskTraceStatus.FAILED, error=str(exc))
            publish_trace_event(on_event, trace, turn=turn, detail="Agent run error.")
            child_result = AgentRunResult(
                context=child_run,
                task_result=RunResult(
                    status=RunStatus.FAILED,
                    error=str(exc),
                    stop_reason=RunStopReason.ERROR,
                ),
            )

        imported_artifacts = self._import_child_artifacts(
            conversation=conversation,
            child_result=child_result,
            tool_call_id=tool_call_id,
        )
        if tool_call_id:
            attach_trace_to_tool_call(assistant_msg, tool_call_id, child_result.context.trace)

        child_trace = child_result.context.trace
        child_message = ""
        if child_trace is not None:
            child_message = str(child_trace.final_message or child_trace.error or child_trace.goal or "").strip()
        message = child_message or child_result.message or "Agent run completed."
        if imported_artifacts:
            lines = ["", "Imported artifacts:"]
            for item in imported_artifacts:
                name = str(item.get("name") or "").strip()
                read_hint = str(item.get("read_hint") or "").strip()
                if name and read_hint:
                    lines.append(f"- {name}: {read_hint}")
                elif name:
                    lines.append(f"- {name}")
            message = message.rstrip() + "\n" + "\n".join(lines)
        is_error = getattr(child_result.task_result, "status", RunStatus.COMPLETED) == RunStatus.FAILED
        if subtask_trace is not None:
            trace = child_result.context.trace
            metadata = getattr(trace, "metadata", {}) if trace is not None else {}
            subtask_trace.record_event(
                kind="subtask",
                phase="end",
                turn=turn,
                node_id=subtask_node_id,
                parent_id=subtask_trace.parent_id,
                name=str(payload.get("mode") or "agent"),
                status="error" if is_error else "completed",
                tool_call_id=tool_call_id or "",
                subtask_id=subtask_id,
                summary=message[:220],
                data={
                    "child_session_id": str((metadata or {}).get("child_session_id") or ""),
                    "tool_count": int(getattr(trace, "tool_count", 0) or 0) if trace is not None else 0,
                    "status": getattr(getattr(child_result, "task_result", None), "status", ""),
                },
            )
        return SubagentExecution(result=ToolResult(message, is_error=is_error), agent_result=child_result)

    @staticmethod
    def _import_child_artifacts(
        *,
        conversation: Conversation,
        child_result: AgentRunResult,
        tool_call_id: str | None,
    ) -> list[dict[str, object]]:
        trace = child_result.context.trace
        if trace is None:
            return []
        metadata = trace.metadata if isinstance(trace.metadata, dict) else {}
        trace.metadata = metadata
        produced_refs = [item for item in (metadata.get("produced_refs") or []) if isinstance(item, dict)]
        artifact_refs = [item for item in produced_refs if str(item.get("type") or "") == "artifact"]
        if not artifact_refs:
            return []

        state = conversation.get_state()
        work_dir = str(
            getattr(conversation, "work_dir", "")
            or getattr(child_result.context.conversation, "work_dir", "")
            or ""
        )
        parent_session_id = getattr(conversation, "id", None)
        child_session_id = str(metadata.get("child_session_id") or getattr(child_result.context.conversation, "id", "") or "")
        current_seq = int(conversation.current_seq_id() or 0) + 1
        imported: list[dict[str, object]] = []
        for ref in artifact_refs:
            source_path = str(ref.get("path") or ref.get("source_path") or "").strip()
            if not source_path:
                continue
            try:
                artifact, created = ArtifactService.import_artifact_file(
                    state,
                    source_path=source_path,
                    work_dir=work_dir,
                    conversation_id=parent_session_id,
                    current_seq=current_seq,
                    provenance={
                        "id": ref.get("id"),
                        "name": ref.get("name") or ref.get("id"),
                        "title": str(getattr(trace, "title", "") or ""),
                        "goal": str(getattr(trace, "goal", "") or ""),
                        "kind": ref.get("kind"),
                        "status": ref.get("status"),
                        "source_session_id": child_session_id,
                        "source_artifact_path": source_path,
                        "source_digest": ref.get("digest"),
                        "agent_run_id": str(getattr(trace, "id", "") or ""),
                        "imported_from": "agent__run",
                        "related": [child_session_id],
                    },
                    data_dir=getattr(conversation, "data_dir", None),
                )
            except Exception as exc:
                logger.debug("Failed to import child artifact %s: %s", source_path, exc)
                continue
            imported.append(
                {
                    "type": "artifact",
                    "name": artifact.name,
                    "kind": artifact.kind,
                    "status": artifact.status,
                    "path": artifact.content_path,
                    "digest": artifact.content_digest,
                    "source_session_id": child_session_id,
                    "source_path": source_path,
                    "source_digest": str(ref.get("digest") or (artifact.frontmatter or {}).get("source_digest") or ""),
                    "read_hint": f'state__artifact(action="read", name="{artifact.name}")',
                    "imported": bool(created),
                }
            )

        if not imported:
            return []
        conversation.set_state(state)
        metadata["imported_artifacts"] = imported
        metadata["handoff_packet"] = {
            "summary": str(getattr(trace, "final_message", "") or "")[:1000],
            "produced_refs": produced_refs,
            "imported_artifacts": imported,
        }
        if tool_call_id:
            metadata["parent_tool_call_id"] = str(tool_call_id or "")
        trace.metadata = metadata
        return imported
