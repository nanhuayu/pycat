from __future__ import annotations

import logging
import threading
from typing import Callable

from core.channel.queue import ChannelQueue
from models.conversation import Message

logger = logging.getLogger(__name__)


class ChannelWorker:
    """Small dispatcher that moves inbound messages out of transport threads."""

    def __init__(
        self,
        *,
        queue: ChannelQueue,
        wake_event: threading.Event,
        stop_event: threading.Event,
        process_message: Callable[[Message], None],
        drain_limit: int = 32,
    ) -> None:
        self._queue = queue
        self._wake_event = wake_event
        self._stop_event = stop_event
        self._process_message = process_message
        self._drain_limit = max(1, int(drain_limit or 32))
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def is_worker_thread(self) -> bool:
        thread = self._thread
        return thread is not None and threading.current_thread() is thread

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run,
                name="PyCat-ChannelGateway",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=0.5)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            for message in self._queue.drain(limit=self._drain_limit):
                try:
                    self._process_message(message)
                except Exception as exc:
                    logger.exception("Channel gateway failed to dispatch inbound message: %s", exc)
