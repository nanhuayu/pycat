from __future__ import annotations

from typing import Any, Optional

from pycat.models.conversation import Conversation


def try_import_conversation_dict(data: Any) -> Optional[Conversation]:
    """Recognize native metadata before trying generic provider payloads."""
    if not isinstance(data, dict):
        return None

    if not isinstance(data.get("messages"), list) or not any(
        key in data for key in ("state", "work_dir", "provider_id", "created_at", "settings")
    ):
        return None

    try:
        return Conversation.from_dict(data)
    except Exception:
        return None
