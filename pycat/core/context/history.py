from __future__ import annotations

import hashlib
import json
from typing import List

from pycat.core.context.sections import normalize_user_message
from pycat.models.conversation import Message, normalize_tool_result


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


def flatten_turn_blocks(blocks: List[List[Message]]) -> List[Message]:
    flattened: List[Message] = []
    for block in blocks:
        flattened.extend(block)
    return flattened


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


def apply_context_window(messages: List[Message], max_messages: int) -> List[Message]:
    """Keep only the last N messages while preferring whole turn blocks."""
    if not isinstance(max_messages, int) or max_messages <= 0:
        return messages
    if len(messages) <= max_messages:
        return messages

    blocks = build_turn_blocks(messages)
    selected: List[List[Message]] = []
    used = 0
    for block in reversed(blocks):
        block_size = len(block)
        if selected and used + block_size > max_messages:
            break
        if not selected and block_size > max_messages:
            selected.append(block[-max_messages:])
            used = max_messages
            break
        selected.append(block)
        used += block_size

    result = flatten_turn_blocks(list(reversed(selected)))
    if not any(m.role == "user" for m in result):
        for msg in reversed(messages):
            if msg.role == "user":
                result.insert(0, msg)
                break
    return result
