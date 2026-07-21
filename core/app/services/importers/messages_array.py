from __future__ import annotations

from typing import Any, Optional

from models.conversation import Conversation, Message


def try_import_messages_array(data: Any) -> Optional[Conversation]:
    """Array of messages format: [{role, content, ...}, ...]"""
    if not isinstance(data, list):
        return None

    messages = [Message.from_dict(m) for m in data if isinstance(m, dict)]
    if not messages:
        return None

    conv = Conversation(messages=messages)
    conv.generate_title_from_first_message()
    return conv
