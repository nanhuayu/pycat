"""Event emission module - handles task event streaming.

Normalizes Agent run events and safely invokes the configured callback.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from pycat.models.contracts.agent import RunEvent, RunEventKind

logger = logging.getLogger(__name__)


class EventEmitter:
    """Handles task event emission with safe callback invocation."""

    def __init__(self, on_event: Optional[Callable[[RunEvent], None]] = None):
        self._on_event = on_event

    def emit(self, kind: RunEventKind, turn: int = 0, **kwargs: Any) -> None:
        """Emit a task event.

        Args:
            kind: Event kind
            turn: Current turn number
            **kwargs: Additional event data
        """
        if not self._on_event:
            return

        try:
            event = RunEvent(kind=kind, turn=turn, **kwargs)
            self._on_event(event)
        except Exception as e:
            logger.debug("Event callback error: %s", e)
