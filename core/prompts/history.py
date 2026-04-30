from __future__ import annotations

from typing import List, Optional

from models.conversation import Message
from core.prompts.user_context import normalize_user_message


CONTROL_MESSAGE_PREFIXES = (
    "[AUTO-CONTINUE]",
    "[WARNING]",
)


def is_control_message(message: Message) -> bool:
    """Return True for runtime-injected user control traffic, not real user intent."""
    if message.role != "user":
        return False
    content = (message.content or "").strip()
    return any(content.startswith(prefix) for prefix in CONTROL_MESSAGE_PREFIXES)


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


def _sanitize_tool_messages(messages: List[Message]) -> List[Message]:
    effective: List[Message] = []
    for msg in messages:
        if msg.role == "tool":
            prev = effective[-1] if effective else None
            if not prev or prev.role not in {"assistant", "tool"}:
                effective.append(
                    Message(
                        role="user",
                        content=f"Tool Output (Recovered Context):\n{msg.content}",
                        tool_call_id=msg.tool_call_id,
                    )
                )
                continue
        effective.append(msg)
    return effective


def get_effective_history(messages: List[Message], keep_last_turns: Optional[int] = None) -> List[Message]:
    """Return messages suitable for sending to the LLM.

    - Filters out condensed/truncated messages.
    - Removes injected runtime control/user-context wrappers.
    - Excludes system messages; the system prompt is assembled separately.
    - Sanitizes orphan tool messages (tool without preceding assistant).
    - Optionally keeps only the last N complete user-led turn blocks.
    - Guarantees at least 1 real user message survives.
    """
    effective: List[Message] = []
    for msg in messages:
        if msg.condense_parent or msg.truncation_parent:
            continue

        if msg.role == "system":
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
            first_keep_index = user_block_indexes[-keep_last_turns]
            blocks = blocks[first_keep_index:]

    effective = _sanitize_tool_messages(flatten_turn_blocks(blocks))

    # 保底: if no real user messages survived, force-keep the latest real user message
    has_user = any(m.role == "user" for m in effective)
    if not has_user:
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
