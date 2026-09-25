from __future__ import annotations

import logging
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

logger = logging.getLogger(__name__)


class ChannelTurnScheduler:
    """Runs one turn per binding in order, with bounded cross-binding concurrency."""

    def __init__(self, *, max_workers: int = 4) -> None:
        self._max_workers = max(1, int(max_workers or 4))
        self._executor: ThreadPoolExecutor | None = None
        self._pending: dict[str, deque[Callable[[], None]]] = {}
        self._active: set[str] = set()
        self._lock = threading.Lock()
        self._stopping = False
        self._generation = 0

    def start(self) -> None:
        with self._lock:
            if self._executor is not None:
                return
            self._generation += 1
            self._stopping = False
            self._executor = ThreadPoolExecutor(
                max_workers=self._max_workers,
                thread_name_prefix="PyCat-ChannelTurn",
            )

    def submit(self, binding_key: str, callback: Callable[[], None]) -> bool:
        key = str(binding_key or "channel:unknown").strip() or "channel:unknown"
        with self._lock:
            if self._stopping or self._executor is None:
                return False
            queue = self._pending.setdefault(key, deque())
            queue.append(callback)
            if key in self._active:
                return True
            self._active.add(key)
            executor = self._executor
            generation = self._generation
        try:
            executor.submit(self._run_next, key, generation)
        except RuntimeError:
            with self._lock:
                self._pending.pop(key, None)
                self._active.discard(key)
        return True

    def stop(self, *, wait: bool = True) -> None:
        with self._lock:
            self._stopping = True
            self._generation += 1
            executor = self._executor
            self._executor = None
            self._pending.clear()
            self._active.clear()
        if executor is not None:
            executor.shutdown(wait=wait, cancel_futures=True)

    def _run_next(self, key: str, generation: int) -> None:
        callback: Callable[[], None] | None = None
        with self._lock:
            if generation != self._generation:
                return
            queue = self._pending.get(key)
            if queue:
                callback = queue.popleft()
        if callback is not None:
            try:
                callback()
            except Exception as exc:
                logger.exception("Channel turn failed for binding %s: %s", key, exc)

        with self._lock:
            if generation != self._generation:
                return
            queue = self._pending.get(key)
            executor = self._executor
            if self._stopping or executor is None or not queue:
                self._pending.pop(key, None)
                self._active.discard(key)
                return
        try:
            executor.submit(self._run_next, key, generation)
        except RuntimeError:
            with self._lock:
                self._pending.pop(key, None)
                self._active.discard(key)


__all__ = ["ChannelTurnScheduler"]
