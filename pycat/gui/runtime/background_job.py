"""Small Qt thread-pool adapter for blocking application operations."""
from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from collections.abc import Callable
from typing import Any

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QObject, QRunnable, pyqtSignal, pyqtSlot

logger = logging.getLogger(__name__)


class BackgroundJobSignals(QObject):
    finished = pyqtSignal(object, object)
    _ready = pyqtSignal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ready.connect(self._deliver)

    @pyqtSlot(object, object)
    def _deliver(self, result, error):
        # Deliver all callbacks on the owning thread before scheduling deletion.
        # A deleteLater connection ahead of queued callbacks can race their emit.
        try:
            self.finished.emit(result, error)
        finally:
            self.deleteLater()


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
        # Qt owns the signal endpoint until queued delivery has finished. A
        # parentless endpoint captured by its callback can otherwise be cyclic
        # garbage, finalized on a worker while Qt is dispatching its proxy.
        self.signals = BackgroundJobSignals(QCoreApplication.instance())
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
            return
        # Application shutdown or explicit teardown can still destroy the
        # endpoint before emission. A dead receiver is a discard; it must not
        # escape run() and abort the process at the Qt virtual boundary.
        try:
            self.signals._ready.emit(result, error)
        except RuntimeError as exc:
            logger.debug("Background job receiver went away before delivery: %s", exc)
            self._notify_discarded(result, error)

    def _notify_discarded(self, result: Any = None, error: Exception | None = None) -> None:
        with self._state_lock:
            if self._discard_notified:
                return
            self._discard_notified = True
        if not sip.isdeleted(self.signals):
            self.signals.deleteLater()
        if self._on_discard is not None:
            try:
                self._on_discard(result, error)
            except Exception:
                return

    def run(self) -> None:
        """Qt virtual boundary: nothing may escape here.

        PyQt turns any exception that leaves ``run()`` into ``qFatal()``, which
        aborts the process immediately with no traceback and no cleanup. Every
        failure is therefore contained and downgraded to a discard.
        """

        try:
            self._run_guarded()
        except BaseException as exc:  # noqa: BLE001 - must never reach Qt
            try:
                logger.exception("Background job failed without delivering a result: %s", exc)
            except BaseException:  # noqa: BLE001 - logging is inside the same Qt boundary
                pass
            try:
                self._notify_discarded(None, exc if isinstance(exc, Exception) else None)
            except BaseException:  # noqa: BLE001 - discard cleanup cannot escape run()
                pass

    def _run_guarded(self) -> None:
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
        except Exception as exc:
            # The operation failed. Report that once; delivery itself must not
            # be retried through the same receiver, which is how the original
            # double-fault escaped run() and aborted the process.
            self._emit_finished(None, exc)
            return
        self._finish(result)
