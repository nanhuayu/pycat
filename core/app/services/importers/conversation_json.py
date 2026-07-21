from __future__ import annotations

from typing import Any, Optional

from models.conversation import Conversation


def try_import_conversation_dict(data: Any) -> Optional[Conversation]:
    """Native Conversation JSON format (has 'messages')."""
    if not isinstance(data, dict):
        return None

    if "messages" not in data:
        return None

    try:
        return Conversation.from_dict(data)
    except Exception:
        return None
