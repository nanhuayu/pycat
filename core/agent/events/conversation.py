"""Conversation patch and context-maintenance event helpers."""

from __future__ import annotations

from typing import Any

from core.agent.events.emitter import EventEmitter
from models.contracts.agent import RunEventKind
from models.conversation import Conversation, Message


def conversation_patch_payload(conversation: Conversation) -> dict[str, Any]:
    condensed: dict[str, str] = {}
    changed_messages: list[Message] = []
    for msg in getattr(conversation, "messages", []) or []:
        changed_messages.append(Message.from_dict(msg.to_dict()))
        parent = str(getattr(msg, "archived_content_id", "") or "").strip()
        msg_id = str(getattr(msg, "id", "") or "").strip()
        if msg_id and parent:
            condensed[msg_id] = parent
    state_payload: dict[str, Any] | None = None
    try:
        state_payload = conversation.get_state().to_dict()
    except Exception:
        state_payload = None
    return {
        "conversation_patch": {
            "conversation_id": str(getattr(conversation, "id", "") or ""),
            "changed_messages": changed_messages,
            "condensed_message_ids": condensed,
            "state": state_payload,
        }
    }


def emit_conversation_patch(
    emitter: EventEmitter | None,
    conversation: Conversation,
    *,
    turn: int = 0,
    detail: str = "Conversation state synchronized.",
    diagnostics: dict[str, Any] | None = None,
) -> None:
    if emitter is None:
        return
    payload = conversation_patch_payload(conversation)
    patch = payload.get("conversation_patch")
    if isinstance(patch, dict) and diagnostics:
        patch["diagnostics"] = dict(diagnostics or {})
    emitter.emit(
        RunEventKind.STEP,
        turn=int(turn or 0),
        detail=detail,
        data=payload,
    )


def emit_condense_report(
    emitter: EventEmitter | None,
    conversation: Conversation,
    report,
    *,
    turn: int = 0,
    live_message_id: str = "",
    live_tool_call_id: str = "",
) -> None:
    if emitter is None or report is None:
        return
    archived = int(getattr(report, "archived_messages", 0) or 0)
    snipped = int(getattr(report, "snipped_messages", 0) or 0)
    archive_updates = int(getattr(report, "archive_updates", 0) or 0)
    memory_updates = int(getattr(report, "memory_updates", 0) or 0)
    summary_updated = bool(getattr(report, "summary_updated", False))
    if not any((archived, snipped, archive_updates, memory_updates, summary_updated)):
        return
    metrics = dict(getattr(report, "metrics", {}) or {})
    state = conversation.get_state()
    history_ids = [
        str(getattr(entry, "id", "") or "")
        for entry in (getattr(state, "archive_index", {}) or {}).values()
        if str(getattr(entry, "kind", "") or "") == "history"
    ][-8:]
    payload = {
        "kind": "context_condense",
        "archived_messages": archived,
        "snipped_messages": snipped,
        "archive_updates": archive_updates,
        "memory_updates": memory_updates,
        "summary_updated": summary_updated,
        "reason": str(getattr(report, "reason", "") or ""),
        "metrics": metrics,
        "history_ids": history_ids,
        "live_message_id": str(live_message_id or ""),
        "live_tool_call_id": str(live_tool_call_id or ""),
    }
    payload.update(conversation_patch_payload(conversation))
    emitter.emit(
        RunEventKind.CONDENSE,
        turn=int(turn or 0),
        detail=(
            f"Context compacted: archived {archived} message(s)."
            if archived
            else "Context maintenance updated compressed views."
        ),
        data=payload,
    )
