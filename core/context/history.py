from __future__ import annotations

from typing import List, Optional

from core.context.sections import normalize_user_message
from models.conversation import Message


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
    """Group active messages into user-led interaction blocks."""
    blocks: List[List[Message]] = []
    current: List[Message] = []
    for msg in messages:
        if msg.role == "user":
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


def count_user_turn_blocks(messages: List[Message]) -> int:
    """Count real user-led turn blocks after removing condensed/control entries."""
    return sum(1 for block in build_turn_blocks(get_effective_history(messages)) if any(msg.role == "user" for msg in block))


def get_effective_history(messages: List[Message], keep_last_turns: Optional[int] = None) -> List[Message]:
    """Return messages suitable for sending to the LLM."""
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

    blocks = build_turn_blocks(effective)
    if keep_last_turns and keep_last_turns > 0:
        user_block_indexes = [
            index for index, block in enumerate(blocks)
            if any(msg.role == "user" for msg in block)
        ]
        if len(user_block_indexes) > keep_last_turns:
            blocks = blocks[user_block_indexes[-keep_last_turns]:]

    effective = flatten_turn_blocks(blocks)
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
