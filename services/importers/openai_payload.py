from __future__ import annotations

from typing import Any, Optional

from models.conversation import Conversation, Message


def try_import_openai_payload(data: Any) -> Optional[Conversation]:
    """OpenAI/OpenRouter request payload format.

    Example:
    {"model": "...", "messages": [{"role":"user","content": ...}], "max_tokens":..., ...}
    """
    if not isinstance(data, dict):
        return None

    if "model" not in data:
        return None

    messages = data.get("messages")
    if not isinstance(messages, list):
        return None

    parsed_messages = [Message.from_dict(m) for m in messages if isinstance(m, dict)]

    conv = Conversation(
        title=data.get("title", "Imported Payload"),
        messages=parsed_messages,
        model=data.get("model", "") or "",
        settings={
            "max_tokens": data.get("max_tokens"),
            "temperature": data.get("temperature"),
            "response_mime_type": data.get("response_mime_type"),
        },
    )

    # Clean None values
    conv.settings = {k: v for k, v in (conv.settings or {}).items() if v is not None}
    if not data.get("title"):
        conv.generate_title_from_first_message()

    return conv
