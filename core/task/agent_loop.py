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

from core.task.agent_run import AgentRunContext, AgentRunParent, AgentRunResult
from core.task.trace import build_trace, publish_trace_event, record_trace_event, update_running_thinking
from core.task.types import RunPolicy, SubtaskTraceStatus, TaskEvent, TaskStatus, TaskStopReason
from core.tools.catalog import ToolSelectionPolicy
from models.conversation import Conversation, Message
from models.provider import Provider, provider_matches_name, split_model_ref

logger = logging.getLogger(__name__)


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
) -> AgentRunContext:
    from core.config.io import load_app_config
    from core.runtime.policy_factory import RuntimePolicyFactory

    request = dict(payload or {})
    request.setdefault("id", f"subtask-{uuid.uuid4().hex[:12]}")
    mode = str(request.get("mode") or "agent")
    message = str(request.get("message") or "")
    trace = build_trace(request)

    child_conversation = Conversation(
        title=f"Agent run: {mode}",
        messages=[Message(role="user", content=message)],
        mode=mode,
    )
    try:
        child_conversation.work_dir = getattr(parent_conversation, "work_dir", ".") or "."
    except Exception as exc:
        logger.debug("Failed to inherit work_dir for nested agent: %s", exc)

    app_settings = {}
    try:
        app_settings = load_app_config().to_dict()
    except Exception:
        pass
    policy = RuntimePolicyFactory.build(
        conversation=child_conversation,
        app_settings=app_settings,
        mode_slug=mode,
        tool_selection=ToolSelectionPolicy.from_dict(request.get("tool_selection")),
        tool_permissions=base_policy.tool_permissions if base_policy is not None else None,
        source="sub_task",
    )
    updates: dict[str, Any] = {}
    model_ref = str(request.get("model_ref") or "").strip()
    if model_ref:
        provider_name, model_name = split_model_ref(model_ref)
        if provider_name and not provider_matches_name(provider, provider_name):
            logger.debug("Ignoring nested agent provider override '%s' because active provider is '%s'", provider_name, getattr(provider, "name", ""))
        if model_name:
            updates["model"] = model_name
    try:
        max_turns = int(request.get("max_turns") or 0)
        if max_turns > 0:
            updates["max_turns"] = max_turns
    except Exception:
        pass
    if request.get("auto_spillover"):
        updates["auto_compress_enabled"] = False
    if base_policy is not None:
        updates.setdefault("temperature", base_policy.temperature)
        updates.setdefault("max_tokens", base_policy.max_tokens)
    policy = replace(policy, **updates)

    runtime_instructions = str(request.get("instructions") or request.get("system_prompt_override") or "").strip()
    if runtime_instructions:
        try:
            child_conversation.set_llm_config(
                child_conversation.get_llm_config().with_updates(system_prompt_override=runtime_instructions)
            )
        except Exception as exc:
            logger.debug("Failed to apply nested agent runtime instructions: %s", exc)

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
    on_event: Callable[[TaskEvent], None] | None = None,
    turn: int = 0,
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
        publish_trace_event(on_event, trace, turn=turn, detail=detail)

    def child_on_event(event: TaskEvent) -> None:
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
    )

    combined_thinking = "".join(pending_thinking).strip()
    if combined_thinking:
        for item in reversed(trace.messages):
            if isinstance(item, dict) and item.get("role") == "assistant" and not str(item.get("thinking") or "").strip():
                item["thinking"] = combined_thinking
                break

    completion_command = ""
    completed = False
    status = result.status
    message = "Agent run completed."
    if result.status == TaskStatus.COMPLETED and result.final_message:
        if combined_thinking and not str(getattr(result.final_message, "thinking", "") or "").strip():
            result.final_message.thinking = combined_thinking
        metadata = getattr(result.final_message, "metadata", {}) or {}
        max_turns_failure = getattr(result, "stop_reason", None) == TaskStopReason.MAX_TURNS
        if not any(item.get("id") == getattr(result.final_message, "id", "") for item in trace.messages if isinstance(item, dict)):
            trace.add_message(result.final_message)
        status = TaskStatus.FAILED if max_turns_failure else result.status
        message = result.final_message.content or "Agent run completed (no output)."
        completion_command = str(metadata.get("completion_command") or "").strip()
        completed = bool(metadata.get("completion")) and not max_turns_failure
        trace.finish(
            SubtaskTraceStatus.FAILED if max_turns_failure else SubtaskTraceStatus.COMPLETED,
            final_message=message,
            error="Agent run reached max turns before completion." if max_turns_failure else "",
        )
        publish("Agent run completed.", force=True)
    elif result.status == TaskStatus.FAILED:
        message = f"Agent run failed: {result.error}"
        trace.finish(SubtaskTraceStatus.FAILED, error=str(result.error or ""))
        publish("Agent run failed.", force=True)
    elif result.status == TaskStatus.CANCELLED:
        message = "Agent run was cancelled."
        trace.finish(SubtaskTraceStatus.CANCELLED, final_message=message)
        publish("Agent run cancelled.", force=True)
    else:
        completed = result.status == TaskStatus.COMPLETED
        trace.finish(SubtaskTraceStatus.COMPLETED, final_message=message)
        publish("Agent run completed.", force=True)

    return AgentRunResult(
        context=run,
        task_result=result.__class__(
            status=status,
            final_message=result.final_message,
            error=result.error,
            stop_reason=getattr(result, "stop_reason", TaskStopReason.COMPLETED),
        ),
        completion_command=completion_command,
        completed=completed,
    )
