from __future__ import annotations

from typing import Any, Optional

from models.conversation import Conversation

from .openai_payload import try_import_openai_payload
from .conversation_json import try_import_conversation_dict
from .chatgpt_export import try_import_chatgpt_export
from .messages_array import try_import_messages_array


def parse_imported_data(data: Any) -> Optional[Conversation]:
    """Parse imported JSON data, handling different formats."""

    # Order matters: more specific formats first.
    for fn in (
        try_import_openai_payload,
        try_import_conversation_dict,
        try_import_chatgpt_export,
        try_import_messages_array,
    ):
        try:
            conv = fn(data)
        except Exception:
            conv = None

        if conv is not None:
            return conv

    return None
