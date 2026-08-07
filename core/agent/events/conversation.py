"""Conversation patch event helpers."""

from __future__ import annotations

from typing import Any

from core.agent.events.emitter import EventEmitter
from models.contracts.agent import RunEventKind
from models.conversation import Conversation, Message


def conversation_patch_payload(
    conversation: Conversation,
    *,
    include_messages: bool = True,
) -> dict[str, Any]:
    condensed: dict[str, str] = {}
    changed_messages: list[Message] = []
    if include_messages:
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
    include_messages: bool = True,
) -> None:
    if emitter is None:
        return
    payload = conversation_patch_payload(
        conversation,
        include_messages=include_messages,
    )
    patch = payload.get("conversation_patch")
    if isinstance(patch, dict) and diagnostics:
        patch["diagnostics"] = dict(diagnostics or {})
    emitter.emit(
        RunEventKind.STEP,
        turn=int(turn or 0),
        detail=detail,
        data=payload,
    )
