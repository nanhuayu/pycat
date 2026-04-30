from __future__ import annotations

from typing import Any, Optional

from models.conversation import Conversation, Message


def try_import_chatgpt_export(data: Any) -> Optional[Conversation]:
    """ChatGPT export format (contains 'mapping')."""
    if not isinstance(data, dict):
        return None

    mapping = data.get("mapping")
    if not isinstance(mapping, dict):
        return None

    messages: list[Message] = []

    for _node_id, node in mapping.items():
        if not isinstance(node, dict):
            continue

        msg_data = node.get("message")
        if not isinstance(msg_data, dict):
            continue

        content_obj = msg_data.get("content")
        if not content_obj:
            continue

        role = (msg_data.get("author") or {}).get("role", "user")
        parts = (content_obj or {}).get("parts", []) if isinstance(content_obj, dict) else []
        if not isinstance(parts, list):
            continue

        content = "\n".join(str(p) for p in parts if isinstance(p, str))
        if content and role in {"user", "assistant"}:
            # Let Message.from_dict normalize fields if needed.
            messages.append(Message(role=role, content=content))

    return Conversation(
        title=data.get("title", "Imported Chat"),
        messages=messages,
    )
