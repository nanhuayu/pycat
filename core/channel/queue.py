from __future__ import annotations

import threading
from collections import deque
from typing import Any

from core.channel.envelope import message_from_channel
from models.conversation import Message


class ChannelQueue:
    """Small in-memory queue used by webhook/MCP/channel adapters."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: deque[Message] = deque()

    def enqueue(self, source: str, content: str, meta: dict[str, Any] | None = None) -> Message:
        message = message_from_channel(source, content, meta)
        with self._lock:
            self._items.append(message)
        return message

    def drain(self, *, limit: int | None = None) -> list[Message]:
        out: list[Message] = []
        with self._lock:
            remaining = None if limit is None else max(0, int(limit))
            while self._items and (remaining is None or remaining > 0):
                out.append(self._items.popleft())
                if remaining is not None:
                    remaining -= 1
        return out

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


__all__ = ["ChannelQueue"]
