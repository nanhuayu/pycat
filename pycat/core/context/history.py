from __future__ import annotations

import copy
import hashlib
import json
from typing import List

from pycat.core.content.archive_store import SessionArchiveStore
from pycat.core.context.sections import normalize_user_message
from pycat.models.conversation import Conversation, Message, normalize_tool_result

CONTROL_MESSAGE_PREFIXES = (
    "[AUTO-CONTINUE]",
    "[WARNING]",
)


def is_control_message(message: Message) -> bool:
    """Return True for runtime-injected user control traffic."""
    if message.role != "user":
        return False
    content = (message.content or "").strip()
    return any(content.startswith(prefix) for prefix in CONTROL_MESSAGE_PREFIXES)


def is_real_user_message(message: Message) -> bool:
    """Return True for persisted user input rather than runtime control data."""
    if message.role != "user" or is_control_message(message):
        return False
    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    return not bool(metadata.get("synthetic"))


def is_restartable_user_message(message: Message, *, allow_external: bool = False) -> bool:
    """Return True when a caller may restart from this real user input.

    External/channel input remains blocked by default.  A bound Channel
    conversation can opt in explicitly; this keeps provenance visible while
    preventing ordinary Desktop sessions from rewriting external traffic.
    """
    if not is_real_user_message(message):
        return False
    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    if allow_external:
        return True
    return not (bool(metadata.get("external_input")) or bool(metadata.get("channel")))


def is_external_user_message(message: Message) -> bool:
    """Return True for a real user message received from an external Channel."""

    if not is_real_user_message(message):
        return False
    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    return bool(metadata.get("external_input") or metadata.get("channel"))


def restartable_user_by_id(
    messages: List[Message],
    user_message_id: str,
    *,
    allow_external: bool = False,
) -> Message | None:
    """Find a restart anchor without crossing an external input by default."""
    target_id = str(user_message_id or "")
    target: Message | None = None
    target_is_external = False
    for message in messages:
        if str(message.id or "") == target_id:
            target = message if is_restartable_user_message(message, allow_external=allow_external) else None
            target_is_external = is_external_user_message(message)
            if target is None:
                return None
            continue
        if (
            target is not None
            and is_real_user_message(message)
            and is_external_user_message(message)
            and (not allow_external or not target_is_external)
        ):
            return None
    return target


def restartable_user_for_assistant(
    messages: List[Message],
    assistant_message_id: str,
    *,
    allow_external: bool = False,
) -> Message | None:
    """Find the local user turn that owns one assistant message."""
    target_id = str(assistant_message_id or "")
    latest_user: Message | None = None
    found = False
    for message in messages:
        if is_real_user_message(message):
            latest_user = (
                message
                if is_restartable_user_message(message, allow_external=allow_external)
                else None
            )
        if message.role == "assistant" and str(message.id or "") == target_id:
            found = True
            break
    if not found or latest_user is None:
        return None
    return restartable_user_by_id(
        messages,
        latest_user.id,
        allow_external=allow_external,
    )


def build_turn_blocks(messages: List[Message]) -> List[List[Message]]:
    """Group messages into blocks anchored by persisted real user input."""
    blocks: List[List[Message]] = []
    current: List[Message] = []
    for msg in messages:
        if is_real_user_message(msg):
            if current:
                blocks.append(current)
            current = [msg]
            continue
        if not current:
            current = [msg]
            continue
        current.append(msg)
    if current:
        blocks.append(current)
    return blocks


def turn_fingerprint(messages: List[Message]) -> str:
    """Return a stable fingerprint of one canonical exact user turn."""
    payload = [_message_fingerprint_fact(message) for message in messages]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode(
        "utf-8",
        errors="replace",
    )
    return hashlib.sha256(encoded).hexdigest()


def _message_fingerprint_fact(message: Message) -> dict:
    metadata = getattr(message, "metadata", {}) or {}
    fact = {
        "id": str(getattr(message, "id", "") or ""),
        "seq_id": int(getattr(message, "seq_id", 0) or 0),
        "role": str(getattr(message, "role", "") or ""),
        "content": str(getattr(message, "content", "") or ""),
        "images": list(getattr(message, "images", []) or []),
        "content_refs": [ref.to_dict() for ref in (getattr(message, "content_refs", []) or [])],
        "tool_calls": [
            _tool_call_fingerprint_fact(tool_call)
            for tool_call in (getattr(message, "tool_calls", None) or [])
            if isinstance(tool_call, dict)
        ],
    }
    stable_metadata = {
        key: metadata[key]
        for key in (
            "external_input",
            "channel",
            "finish_reason",
            "incomplete",
            "incomplete_reason",
            "interrupted",
            "interrupt_reason",
            "runtime_error",
        )
        if key in metadata
    }
    if stable_metadata:
        fact["metadata"] = stable_metadata
    return fact


def _tool_call_fingerprint_fact(tool_call: dict) -> dict:
    function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
    fact = {
        "id": str(tool_call.get("id") or ""),
        "type": str(tool_call.get("type") or ""),
        "function": {
            "name": str(function.get("name") or tool_call.get("name") or ""),
            "arguments": function.get("arguments", tool_call.get("arguments", "")),
        },
    }
    if tool_call.get("result") is None:
        fact["result"] = None
        return fact

    result = normalize_tool_result(tool_call.get("result"))
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    content_id = str(metadata.get("content_id") or "").strip()
    result_fact = {
        "type": str(result.get("type") or "tool_result"),
        "is_error": bool(metadata.get("is_error")),
        "error_code": str(metadata.get("error_code") or ""),
        "retryable": bool(metadata.get("retryable")),
    }
    if content_id:
        result_fact["content_id"] = content_id
    else:
        result_fact["content"] = result.get("content")
        if result.get("images"):
            result_fact["images"] = list(result.get("images") or [])
        if result.get("run") is not None:
            result_fact["run"] = result.get("run")
    for key in ("source_ref", "references", "file_change"):
        if metadata.get(key) not in (None, "", []):
            result_fact[key] = metadata.get(key)
    fact["result"] = result_fact
    return fact


def count_user_turn_blocks(messages: List[Message]) -> int:
    """Count real user-led turn blocks after removing condensed/control entries."""
    return sum(1 for block in build_turn_blocks(get_effective_history(messages)) if any(msg.role == "user" for msg in block))


def history_projection_signature(conversation: Conversation) -> str:
    """Identify compact/user changes without hashing large tool bodies on UI refresh.

    Appending assistant output or creating a dormant capsule leaves the last
    request estimate usable. Selecting a capsule/checkpoint or a new user input
    changes the projection and invalidates that estimate.
    """
    messages = conversation.messages
    latest_user = next((message.id for message in reversed(messages) if is_real_user_message(message)), "")
    facts = (
        latest_user,
        str(conversation.get_state().summary or ""),
        [(message.id, message.archived_content_id) for message in messages if message.archived_content_id],
    )
    return hashlib.sha256(json.dumps(facts, ensure_ascii=False).encode("utf-8")).hexdigest()


def project_history(conversation: Conversation, *, messages: list[Message] | None = None) -> list[Message]:
    """Project exact history and selected capsules for both requests and savings checks."""
    store = SessionArchiveStore(conversation.work_dir, conversation.id, data_dir=getattr(conversation, "data_dir", None))
    projected: list[Message] = []
    for block in build_turn_blocks(list(conversation.messages if messages is None else messages)):
        if not block or not is_real_user_message(block[0]):
            projected.extend(copy.deepcopy(message) for message in get_effective_history(block))
            continue
        user = block[0]
        if user.archived_content_id:
            continue
        ref = (user.metadata or {}).get("turn_capsule_ref") if isinstance(user.metadata, dict) else None
        content_id = str(ref.get("content_id") or "") if isinstance(ref, dict) else ""
        tail = block[1:]
        selected = bool(tail and content_id) and all(
            str(getattr(message, "archived_content_id", "") or "") == content_id
            for message in tail
        )
        record = store.read_record(content_id, kind="history") if selected else None
        metadata = record.metadata if record is not None and isinstance(record.metadata, dict) else {}
        valid = bool(
            record is not None
            and record.summary
            and metadata.get("scope") == "turn_capsule"
            and isinstance(ref, dict)
            and metadata.get("fingerprint") == ref.get("fingerprint")
        )
        fingerprint = turn_fingerprint(block) if valid else ""
        valid = valid and ref.get("fingerprint") == fingerprint
        if not valid:
            exact = copy.deepcopy(block)
            for message in exact:
                if message is not exact[0] and str(message.archived_content_id or "") == content_id:
                    message.archived_content_id = None
            projected.extend(get_effective_history(exact))
            continue
        projected.extend(get_effective_history([copy.deepcopy(user)]))
        projected.append(
            Message(
                role="assistant",
                content=record.summary,
                metadata={
                    "synthetic": True,
                    "context_kind": "turn_capsule",
                    "content_id": record.id,
                    "fingerprint": fingerprint,
                    "coverage": {
                        "start_seq": int(metadata.get("start_seq", 0) or 0),
                        "end_seq": int(metadata.get("end_seq", 0) or 0),
                    },
                    "trust": "mixed_provenance",
                },
            )
        )
    return projected


def get_effective_history(messages: List[Message]) -> List[Message]:
    """Return every active canonical message suitable for provider projection."""
    effective: List[Message] = []
    for msg in messages:
        if msg.archived_content_id:
            continue
        if msg.role == "system":
            continue
        if msg.role == "tool":
            continue
        normalized = normalize_user_message(msg) if msg.role == "user" else msg
        if normalized.role == "user" and not (normalized.content or "").strip():
            continue
        if is_control_message(normalized):
            continue
        effective.append(normalized)

    if not any(m.role == "user" for m in effective):
        for msg in reversed(messages):
            normalized = normalize_user_message(msg) if msg.role == "user" else msg
            if normalized.role == "user" and not is_control_message(normalized):
                if (normalized.content or "").strip():
                    effective.append(normalized)
                break
    return effective
