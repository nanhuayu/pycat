"""Small Qt thread-pool adapter for blocking application operations."""
from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal


class BackgroundJobSignals(QObject):
    finished = pyqtSignal(object, object)


class BackgroundJob(QRunnable):
    """Run one callable off the Qt thread and return through a queued signal."""

    def __init__(
        self,
        operation: Callable[[], Any],
        *,
        on_discard: Callable[[Any, Exception | None], None] | None = None,
    ) -> None:
        super().__init__()
        self._operation = operation
        self._on_discard = on_discard
        self.signals = BackgroundJobSignals()
        self._cancel_event = threading.Event()
        self._state_lock = threading.Lock()
        self._abandoned = False
        self._started = False
        self._discard_notified = False

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def cancel(self) -> None:
        """Request cancellation and suppress a late completion callback.

        A ``QRunnable`` cannot interrupt arbitrary synchronous Python work.  The
        operation may observe ``cancelled`` when it has a cooperative boundary;
        regardless of cooperation, cancellation prevents a callback from
        reaching a window that is already closing.
        """

        with self._state_lock:
            self._cancel_event.set()
            notify_now = not self._started
        if notify_now:
            self._notify_discarded()

    def abandon(self) -> None:
        """Drop the result when the owning Qt object is going away.

        A queued runnable may never get a chance to enter ``run()`` during
        application shutdown. Release its owner immediately in that case;
        running work still performs the same cleanup after it returns.
        """

        with self._state_lock:
            self._abandoned = True
            self._cancel_event.set()
            notify_now = not self._started
        if notify_now:
            self._notify_discarded()

    def _may_emit(self) -> bool:
        with self._state_lock:
            return not self._abandoned and not self._cancel_event.is_set()

    def _finish(self, result: Any) -> None:
        with self._state_lock:
            discarded = self._abandoned or self._cancel_event.is_set()
        if not discarded:
            self._emit_finished(result, None)
            return
        self._notify_discarded(result, None)

    def _emit_finished(self, result: Any, error: Exception | None) -> None:
        with self._state_lock:
            if self._abandoned or self._cancel_event.is_set():
                discarded = True
            else:
                discarded = False
        if discarded:
            self._notify_discarded(result, error)
        else:
            self.signals.finished.emit(result, error)

    def _notify_discarded(self, result: Any = None, error: Exception | None = None) -> None:
        with self._state_lock:
            if self._discard_notified:
                return
            self._discard_notified = True
        if self._on_discard is not None:
            try:
                self._on_discard(result, error)
            except Exception:
                return

    def run(self) -> None:
        with self._state_lock:
            self._started = True
            should_run = not self._abandoned and not self._cancel_event.is_set()
        if not should_run:
            self._notify_discarded()
            return
        try:
            result = self._operation()
            if inspect.isawaitable(result):
                result = asyncio.run(result)
            self._finish(result)
        except Exception as exc:
            if self._may_emit():
                self._emit_finished(None, exc)
            else:
                self._notify_discarded(None, exc)
