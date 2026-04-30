"""Event emission module - handles task event streaming.

Extracted from task.py to reduce complexity.
Responsibilities:
- Emit task events to callbacks
- Handle event formatting
- Safe callback invocation with error handling
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from core.task.types import TaskEvent, TaskEventKind

logger = logging.getLogger(__name__)


class EventEmitter:
    """Handles task event emission with safe callback invocation."""

    def __init__(self, on_event: Optional[Callable[[TaskEvent], None]] = None):
        self._on_event = on_event

    def emit(self, kind: TaskEventKind, turn: int = 0, **kwargs: Any) -> None:
        """Emit a task event.

        Args:
            kind: Event kind
            turn: Current turn number
            **kwargs: Additional event data
        """
        if not self._on_event:
            return

        try:
            event = TaskEvent(kind=kind, turn=turn, **kwargs)
            self._on_event(event)
        except Exception as e:
            logger.debug("Event callback error: %s", e)
