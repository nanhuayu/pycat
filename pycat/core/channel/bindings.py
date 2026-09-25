from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ChannelConversationBindingStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict[str, str]:
        try:
            if self._path.exists() and self._path.is_file():
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return {
                        str(key): str(value)
                        for key, value in raw.items()
                        if str(key).strip() and str(value).strip()
                    }
        except Exception as exc:
            logger.debug("Failed to load channel binding store: %s", exc)
        return {}

    def get(self, channel_id: str, user_id: str) -> str:
        key = channel_binding_key(channel_id, user_id)
        with self._lock:
            return str(self._data.get(key, "") or "")

    def set(self, channel_id: str, user_id: str, conversation_id: str) -> None:
        key = channel_binding_key(channel_id, user_id)
        value = str(conversation_id or "").strip()
        if not key or not value:
            return
        with self._lock:
            self._data[key] = value
            self._save_locked()

    def items(self) -> dict[str, str]:
        with self._lock:
            return dict(self._data)

    def remove(self, channel_id: str, user_id: str) -> bool:
        key = channel_binding_key(channel_id, user_id)
        if not key:
            return False
        with self._lock:
            if key not in self._data:
                return False
            del self._data[key]
            self._save_locked()
            return True

    def prune(
        self,
        *,
        valid_channel_ids: set[str] | None = None,
        valid_conversation_ids: set[str] | None = None,
    ) -> int:
        channels = {str(item or "").strip() for item in (valid_channel_ids or set()) if str(item or "").strip()}
        conversations = {str(item or "").strip() for item in (valid_conversation_ids or set()) if str(item or "").strip()}
        removed = 0
        with self._lock:
            for key, value in list(self._data.items()):
                channel_id = str(key).split("::", 1)[0].strip()
                conversation_id = str(value or "").strip()
                if channels and channel_id not in channels:
                    del self._data[key]
                    removed += 1
                    continue
                if conversations and conversation_id not in conversations:
                    del self._data[key]
                    removed += 1
            if removed:
                self._save_locked()
        return removed

    def _save_locked(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.debug("Failed to save channel binding store: %s", exc)


def channel_binding_key(channel_id: str, user_id: str) -> str:
    return f"{str(channel_id or '').strip()}::{str(user_id or '').strip()}"


def is_bound_channel_conversation(conversation: Any) -> bool:
    """Return whether a conversation has an explicit Channel binding.

    Revision of external user turns is allowed only for a complete persisted
    binding.  A mode label or message provenance alone is not sufficient.
    """

    settings = getattr(conversation, "settings", {}) or {}
    binding = settings.get("channel_binding") if isinstance(settings, dict) else None
    if not isinstance(binding, dict):
        return False
    return bool(
        str(binding.get("channel_id") or "").strip()
        and str(binding.get("source") or "").strip()
    )


__all__ = [
    "ChannelConversationBindingStore",
    "channel_binding_key",
    "is_bound_channel_conversation",
]
