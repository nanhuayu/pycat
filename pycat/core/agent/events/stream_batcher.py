from __future__ import annotations

from collections.abc import Callable
from typing import Any


class StreamDeltaBatcher:
    """Coalesce visible and reasoning deltas without owning run state."""

    def __init__(
        self,
        *,
        schedule: Callable[[float, Callable[[], None]], Any],
        emit: Callable[[str, str], None],
        interval_seconds: float = 0.6,
    ) -> None:
        self._schedule = schedule
        self._emit = emit
        self._interval_seconds = max(0.0, float(interval_seconds))
        self._visible_parts: list[str] = []
        self._thinking_parts: list[str] = []
        self._handle: Any = None

    def append_visible(self, text: str) -> None:
        if text:
            self._visible_parts.append(str(text))
            self._ensure_scheduled()

    def append_thinking(self, text: str) -> None:
        if text:
            self._thinking_parts.append(str(text))
            self._ensure_scheduled()

    def flush(self) -> None:
        handle = self._handle
        self._handle = None
        cancel = getattr(handle, "cancel", None)
        if callable(cancel):
            cancel()
        if not self._visible_parts and not self._thinking_parts:
            return
        visible = "".join(self._visible_parts)
        thinking = "".join(self._thinking_parts)
        self._visible_parts.clear()
        self._thinking_parts.clear()
        self._emit(visible, thinking)

    def _ensure_scheduled(self) -> None:
        if self._handle is None:
            self._handle = self._schedule(self._interval_seconds, self.flush)
