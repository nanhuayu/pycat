"""Unified nested agent execution loop.

Root tasks and delegated children share the same ``Task.run`` loop. This module
only builds a normalized child ``AgentRunContext`` and adapts its event stream
into the parent trace sink.
"""
from __future__ import annotations

from dataclasses import replace
import logging
import time
import uuid
from typing import Any, Callable

from core.agent.run.state import AgentRunContext, AgentRunParent, AgentRunResult
from core.agent.events.trace import build_trace, publish_trace_event, record_trace_event, update_running_thinking
from models.contracts.agent import RunPolicy, SubtaskTraceStatus, RunEvent, RunStatus, RunStopReason
from core.agent.events.debug_trace import DebugTraceContext
from models.contracts.tooling import ToolSelectionPolicy
from models.conversation import Conversation, Message
from core.llm.model_selection import resolve_model_target
from models.provider import Provider

logger = logging.getLogger(__name__)


def _normalize_context_refs(value: Any) -> list[Any]:
    refs = value if isinstance(value, list) else []
    normalized: list[Any] = []
    for item in refs:
        if isinstance(item, str):
            text = item.strip()
            if text:
                normalized.append(text)
            continue
        if isinstance(item, dict):
            cleaned = {
                str(key): val
                for key, val in item.items()
                if str(key).strip() and val not in (None, "")
            }
            if cleaned:
                normalized.append(cleaned)
    return normalized


def _format_context_ref(item: Any) -> str:
    if isinstance(item, dict):
        ref_type = str(item.get("type") or item.get("kind") or "ref").strip() or "ref"
        value = str(
            item.get("id")
            or item.get("content_id")
            or item.get("artifact")
            or item.get("path")
            or item.get("url")
            or ""
        ).strip()
        desc = str(item.get("description") or item.get("title") or "").strip()
        label = f"{ref_type}:{value}" if value else ref_type
        return f"{label} - {desc}" if desc else label
    return str(item or "").strip()


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except Exception:
        return None
    return number if number > 0 else None


def _append_parent_context_notice(
    message: str,
    *,
    parent_session_id: str,
    shared_context_policy: str,
    context_refs: list[Any],
) -> str:
    lines = [
        "Parent session context:",
        f"- parent_session_id: {parent_session_id or '-'}",
        f"- shared_context_policy: {shared_context_policy or 'indexes_only'}",
        "- You may use state__artifact(action=\"list\"|\"read\") for shared artifacts and archive__list/archive__read for shared archived content.",
        "- Do not assume access to the parent's full conversation unless an explicit context_ref is provided.",
    ]
    if context_refs:
        lines.append("- explicit context_refs:")
        lines.extend(f"  - {_format_context_ref(item)}" for item in context_refs)
    return "\n\n".join([str(message or "").strip(), "\n".join(lines)]).strip()


def _artifact_index_snapshot(artifacts: dict[str, Any]) -> dict[str, Any]:
    index: dict[str, Any] = {}
    for name, artifact in (artifacts or {}).items():
        if isinstance(artifact, dict):
            item = dict(artifact)
        else:
            try:
                item = artifact.to_dict()
            except Exception:
                item = {"name": str(name or "")}
        item["content"] = ""
        index[str(name)] = item
    return index


def _shared_parent_state(parent_conversation: Conversation, shared_context_policy: str) -> tuple[dict[str, Any], set[str], set[str]]:
    """Return a read-oriented state snapshot for a child run plus baseline ids."""
    try:
        source = parent_conversation.get_state().to_dict()
    except Exception:
        source = dict(getattr(parent_conversation, "_state_dict", {}) or {})
    if not isinstance(source, dict):
        source = {}

    artifacts = dict(source.get("artifacts") or {}) if isinstance(source.get("artifacts"), dict) else {}
    archive_index = dict(source.get("archive_index") or {}) if isinstance(source.get("archive_index"), dict) else {}
    baseline_artifacts = {str(key) for key in artifacts.keys()}
    baseline_archives = {str(key) for key in archive_index.keys()}

    policy = str(shared_context_policy or "indexes_only").strip().lower() or "indexes_only"
    if policy == "full_session_readonly":
        return dict(source), baseline_artifacts, baseline_archives

    shared: dict[str, Any] = {
        "artifacts": artifacts if policy == "selected_artifacts" else _artifact_index_snapshot(artifacts),
        "archive_index": archive_index,
        "last_updated_seq": int(source.get("last_updated_seq") or 0),
        "state_version": int(source.get("state_version") or 0),
    }
    if policy == "selected_artifacts":
        shared["summary"] = str(source.get("summary") or "")
        if isinstance(source.get("work_trace"), dict):
            shared["work_trace"] = dict(source.get("work_trace") or {})
    return shared, baseline_artifacts, baseline_archives


def _apply_shared_parent_state(
    *,
    child_conversation: Conversation,
    parent_conversation: Conversation,
    request: dict[str, Any],
    shared_context_policy: str,
) -> None:
    try:
        from models.contracts.session_state import SessionState

        shared_state, baseline_artifacts, baseline_archives = _shared_parent_state(
            parent_conversation,
            shared_context_policy,
        )
        child_conversation.set_state(SessionState.from_dict(shared_state))
        request["_parent_artifact_names"] = sorted(baseline_artifacts)
        request["_parent_archive_ids"] = sorted(baseline_archives)
    except Exception as exc:
        logger.debug("Failed to copy parent context indexes for nested agent: %s", exc)


def _collect_produced_refs(run: AgentRunContext) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    metadata = dict(run.metadata or {})
    baseline_artifacts = {str(item) for item in metadata.get("_parent_artifact_names") or []}
    baseline_archives = {str(item) for item in metadata.get("_parent_archive_ids") or []}
    try:
        state = run.conversation.get_state()
        for name, artifact in (state.artifacts or {}).items():
            artifact_name = str(name or "").strip()
            if not artifact_name or artifact_name in baseline_artifacts:
                continue
            refs.append(
                {
                    "type": "artifact",
                    "id": artifact_name,
                    "name": artifact_name,
                    "kind": str(getattr(artifact, "kind", "") or ""),
                    "status": str(getattr(artifact, "status", "") or ""),
                    "path": str(getattr(artifact, "content_path", "") or ""),
                    "source_session_id": str(getattr(run.conversation, "id", "") or ""),
                    "digest": str(getattr(artifact, "content_digest", "") or ""),
                    "chars": int(getattr(artifact, "content_chars", 0) or 0),
                }
            )
        for content_id, record in (state.archive_index or {}).items():
            archive_id = str(content_id or "").strip()
            if not archive_id or archive_id in baseline_archives:
                continue
            refs.append(
                {
                    "type": "content",
                    "id": archive_id,
                    "kind": str(getattr(record, "kind", "") or ""),
                    "source": str(getattr(record, "source", "") or ""),
                    "ref": str(getattr(record, "original_ref", "") or ""),
                }
            )
    except Exception as exc:
        logger.debug("Failed to collect nested agent produced refs: %s", exc)
    return refs


def _sync_trace_runtime_metadata(trace, run: AgentRunContext) -> None:
    metadata = trace.metadata if isinstance(trace.metadata, dict) else {}
    trace.metadata = metadata
    metadata.setdefault("agent_id", str(run.mode or ""))
    metadata.setdefault("mode", str(run.mode or ""))
    metadata.setdefault("child_session_id", str(getattr(run.conversation, "id", "") or ""))
    metadata.setdefault("parent_session_id", str((run.metadata or {}).get("parent_session_id") or ""))
    profile_max_turns = _positive_int((run.metadata or {}).get("profile_max_turns"))
    if profile_max_turns is not None:
        metadata["profile_max_turns"] = profile_max_turns
    effective_max_turns = _positive_int(getattr(run.policy, "max_turns", None))
    if effective_max_turns is not None:
        metadata["effective_max_turns"] = effective_max_turns
    metadata["tool_count"] = int(getattr(trace, "tool_count", 0) or 0)
    metadata["message_count"] = len([item for item in (getattr(trace, "messages", []) or []) if isinstance(item, dict)])


def build_root_run(*, conversation: Conversation, provider: Provider, policy: RunPolicy) -> AgentRunContext:
    return AgentRunContext(
        id=str(getattr(conversation, "id", "") or f"agent-{uuid.uuid4().hex[:12]}"),
        kind="root",
        mode=str(policy.mode or getattr(conversation, "mode", "chat") or "chat"),
        depth=0,
        conversation=conversation,
        provider=provider,
        policy=policy,
    )


def build_child_run(
    *,
    payload: dict[str, Any],
    parent_conversation: Conversation,
    provider: Provider,
    base_policy: RunPolicy | None = None,
    app_settings: dict[str, Any] | None = None,
    providers: list[Provider] | tuple[Provider, ...] = (),
) -> AgentRunContext:
    from core.modes.manager import ModeManager
    from core.agent.policy import RunPolicyBuilder

    request = dict(payload or {})
    request.setdefault("id", f"subtask-{uuid.uuid4().hex[:12]}")
    mode = str(request.get("mode") or "agent")
    parent_work_dir = getattr(parent_conversation, "work_dir", ".") or "."
    mode_manager = ModeManager(str(parent_work_dir or "."))
    mode_cfg = mode_manager.find(mode)
    if mode_cfg is None or not mode_cfg.is_subagent_profile():
        raise ValueError(f"Unknown sub-agent profile: {mode}")
    profile_max_turns = _positive_int(getattr(mode_cfg, "max_turns", None))

    shared_context_policy = str(
        request.get("shared_context_policy")
        or getattr(mode_cfg, "shared_context_policy", "")
        or "indexes_only"
    ).strip() or "indexes_only"
    context_refs = _normalize_context_refs(request.get("context_refs"))
    parent_session_id = str(getattr(parent_conversation, "id", "") or "")
    request["context_refs"] = context_refs
    request["parent_session_id"] = parent_session_id
    request["shared_context_policy"] = shared_context_policy
    request["profile"] = mode
    if profile_max_turns is not None:
        request["profile_max_turns"] = profile_max_turns
    message = _append_parent_context_notice(
        str(request.get("message") or ""),
        parent_session_id=parent_session_id,
        shared_context_policy=shared_context_policy,
        context_refs=context_refs,
    )
    request["message"] = message

    child_conversation = Conversation(
        title=str(request.get("title") or f"Agent run: {mode}"),
        messages=[Message(role="user", content=message)],
        mode=mode,
        settings={
            "parent_session_id": parent_session_id,
            "shared_context_policy": shared_context_policy,
            "context_refs": context_refs,
            "session_instructions": str(request.get("instructions") or "").strip(),
        },
    )
    child_session_id = str(getattr(child_conversation, "id", "") or "")
    request["child_session_id"] = child_session_id
    try:
        child_conversation.settings["child_session_id"] = child_session_id
    except Exception:
        pass
    try:
        child_conversation.work_dir = parent_work_dir
    except Exception as exc:
        logger.debug("Failed to inherit work_dir for nested agent: %s", exc)
    _apply_shared_parent_state(
        child_conversation=child_conversation,
        parent_conversation=parent_conversation,
        request=request,
        shared_context_policy=shared_context_policy,
    )

    request_selection = ToolSelectionPolicy.from_dict(request.get("tool_selection"))
    if base_policy is not None:
        request_selection = base_policy.tool_selection.intersect(request_selection)
    policy = RunPolicyBuilder.build(
        conversation=child_conversation,
        app_settings=app_settings or {},
        mode_slug=mode,
        tool_selection=request_selection,
        tool_permissions=base_policy.tool_permissions if base_policy is not None else None,
        mode_manager=mode_manager,
        source="sub_task",
    )
    updates: dict[str, Any] = {}
    selection = resolve_model_target(
        list(providers or ()) + [provider],
        getattr(mode_cfg, "model_target", None),
        primary_provider=provider,
        primary_model=(
            str(getattr(base_policy, "model", "") or "").strip()
            or str(getattr(parent_conversation, "model", "") or "").strip()
        ),
        auxiliary_model_ref=str((app_settings or {}).get("default_auxiliary_model", "") or "").strip(),
    )
    if selection.fallback_from:
        logger.warning(
            "Subagent profile %s %s model is unavailable; using %s model %s",
            mode,
            selection.fallback_from,
            selection.source or "fallback",
            selection.model or "<empty>",
        )
    provider = selection.provider or provider
    if selection.model:
        updates["model"] = selection.model
        request["model_ref"] = f"{getattr(provider, 'name', '')}|{selection.model}"
        child_conversation.model = selection.model
        child_conversation.provider_id = str(getattr(provider, "id", "") or "")
        child_conversation.provider_name = str(getattr(provider, "name", "") or "")
    if request.get("auto_spillover"):
        updates["auto_compress_enabled"] = False
    if base_policy is not None:
        updates.setdefault("temperature", base_policy.temperature)
        updates.setdefault("max_tokens", base_policy.max_tokens)
    policy = replace(policy, **updates)
    effective_max_turns = _positive_int(getattr(policy, "max_turns", None))
    if effective_max_turns is not None:
        request["effective_max_turns"] = effective_max_turns
    trace = build_trace(request)

    parent = AgentRunParent(
        message_id=str(request.get("parent_message_id") or ""),
        tool_call_id=str(request.get("parent_tool_call_id") or request.get("tool_call_id") or ""),
        root_tool_call_id=str(request.get("root_tool_call_id") or request.get("tool_call_id") or ""),
    )
    return AgentRunContext(
        id=str(request.get("id") or trace.id),
        kind=str(request.get("kind") or "subagent"),
        mode=mode,
        depth=int(request.get("depth") or 0),
        conversation=child_conversation,
        provider=provider,
        policy=policy,
        parent=parent,
        trace=trace,
        metadata=request,
    )


async def run_child_agent(
    *,
    task,
    run: AgentRunContext,
    approval_callback=None,
    questions_callback=None,
    cancel_event=None,
    on_event: Callable[[RunEvent], None] | None = None,
    turn: int = 0,
    debug_trace: DebugTraceContext | None = None,
) -> AgentRunResult:
    trace = run.trace
    if trace is None:
        raise ValueError("Nested AgentRunContext requires a trace")

    try:
        trace.add_message(run.conversation.messages[0])
    except Exception as exc:
        logger.debug("Failed to record nested agent prompt: %s", exc)
    publish_trace_event(on_event, trace, turn=turn, detail=f"Agent run {trace.title} prompt recorded.")

    pending_thinking: list[str] = []
    last_publish_at = 0.0

    def publish(detail: str = "Agent run updated.", *, force: bool = False) -> None:
        nonlocal last_publish_at
        now = time.monotonic()
        if not force and (now - last_publish_at) < 1.0:
            return
        last_publish_at = now
        _sync_trace_runtime_metadata(trace, run)
        publish_trace_event(on_event, trace, turn=turn, detail=detail)

    def child_on_event(event: RunEvent) -> None:
        try:
            event.source = "subtask"
            event.subtask_id = trace.id
            if run.parent is not None:
                event.parent_message_id = run.parent.message_id
                event.parent_tool_call_id = run.parent.tool_call_id
                event.root_tool_call_id = run.parent.root_tool_call_id or run.parent.tool_call_id
            record_trace_event(trace, event)
            publish(str(getattr(event, "detail", "") or "Agent run updated."))
        except Exception as exc:
            logger.debug("Failed to record nested agent event: %s", exc)

    def child_on_thinking(thinking: str) -> None:
        text = str(thinking or "")
        if not text:
            return
        pending_thinking.append(text)
        update_running_thinking(trace, "".join(pending_thinking))
        publish("Agent run thinking updated.")

    result = await task.run(
        provider=run.provider,
        conversation=run.conversation,
        policy=run.policy,
        on_event=child_on_event,
        on_token=None,
        on_thinking=child_on_thinking,
        approval_callback=approval_callback,
        questions_callback=questions_callback,
        cancel_event=cancel_event,
        debug_trace=debug_trace,
    )

    combined_thinking = "".join(pending_thinking).strip()
    if combined_thinking:
        for item in reversed(trace.messages):
            if isinstance(item, dict) and item.get("role") == "assistant" and not str(item.get("thinking") or "").strip():
                item["thinking"] = combined_thinking
                break

    completed = False
    status = result.status
    result_conversation = getattr(result, "conversation", None) or run.conversation
    final_message = getattr(result, "final_message", None)
    if final_message is None:
        final_message = next(
            (
                item
                for item in reversed(getattr(result_conversation, "messages", []) or [])
                if getattr(item, "role", "") == "assistant" and str(getattr(item, "content", "") or "").strip()
            ),
            None,
        )
    message = "Agent run completed."
    interrupted = result.status == RunStatus.INTERRUPTED
    stop_reason = getattr(result, "stop_reason", RunStopReason.COMPLETED)
    max_turns_interruption = interrupted and stop_reason == RunStopReason.MAX_TURNS
    produced_refs = _collect_produced_refs(run)
    if produced_refs:
        trace.metadata["produced_refs"] = produced_refs
    if interrupted:
        trace.metadata.update(
            {
                "partial": True,
                "partial_status": "interrupted",
                "partial_reason": str(getattr(stop_reason, "value", stop_reason) or "interrupted"),
            }
        )
    if max_turns_interruption:
        trace.metadata["suggested_retry"] = {
            "action": "increase_subagent_profile_budget_or_narrow_goal",
            "effective_max_turns": int(getattr(run.policy, "max_turns", 0) or 0),
            "narrow_goal": True,
        }

    if result.status in {RunStatus.COMPLETED, RunStatus.INTERRUPTED}:
        if final_message is not None:
            if combined_thinking and not str(getattr(final_message, "thinking", "") or "").strip():
                final_message.thinking = combined_thinking
            if not any(item.get("id") == getattr(final_message, "id", "") for item in trace.messages if isinstance(item, dict)):
                trace.add_message(final_message)
            message = final_message.content or "Agent run completed (no output)."
        elif interrupted:
            message = str(result.error or "Agent run interrupted before producing a final response.").strip()
        status = result.status
        if interrupted:
            trace.metadata["partial_result"] = message
        completed = result.status == RunStatus.COMPLETED
        trace.finish(
            SubtaskTraceStatus.INTERRUPTED if interrupted else SubtaskTraceStatus.COMPLETED,
            final_message=message,
            error="",
        )
        publish("Agent run interrupted." if interrupted else "Agent run completed.", force=True)
    elif result.status == RunStatus.FAILED:
        message = str(result.error or "Agent run failed.").strip()
        trace.finish(SubtaskTraceStatus.FAILED, error=message)
        publish("Agent run failed.", force=True)
    elif result.status == RunStatus.CANCELLED:
        message = "Agent run was cancelled."
        trace.finish(SubtaskTraceStatus.CANCELLED, final_message=message)
        publish("Agent run cancelled.", force=True)
    else:
        completed = result.status == RunStatus.COMPLETED
        trace.finish(SubtaskTraceStatus.COMPLETED, final_message=message)
        publish("Agent run completed.", force=True)

    return AgentRunResult(
        context=run,
        task_result=result.__class__(
            status=status,
            final_message=final_message,
            error=result.error,
            stop_reason=stop_reason,
            conversation=result_conversation,
        ),
        completed=completed,
    )
