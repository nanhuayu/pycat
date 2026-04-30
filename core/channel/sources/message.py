from __future__ import annotations

from models.conversation import Message


def channel_value(message: Message, key: str) -> str:
    metadata = getattr(message, "metadata", {}) or {}
    channel_meta = metadata.get("channel") if isinstance(metadata, dict) else None
    if not isinstance(channel_meta, dict):
        return ""
    return str(channel_meta.get(key, "") or "").strip()


__all__ = ["channel_value"]