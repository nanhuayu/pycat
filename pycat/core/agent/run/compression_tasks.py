"""Run-local scheduling for derived Archive summaries."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


class CompressionTaskSet:
    """Deduplicate and bound summary work owned by one Agent run."""

    def __init__(
        self,
        summarize: Callable[[str], Awaitable[Any]],
        *,
        concurrency: int = 3,
        cancel_event: Any = None,
    ) -> None:
        limit = max(2, int(concurrency or 3))
        self._summarize = summarize
        self._total_slots = asyncio.Semaphore(limit)
        self._background_slots = asyncio.Semaphore(limit - 1)
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._priorities: dict[str, bool] = {}
        self._running: set[str] = set()
        self._superseded: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._cancel_event = cancel_event
        self.tight_pressure = False

    def submit(self, content_id: str, *, priority: str = "background") -> None:
        key = str(content_id or "").strip()
        if not key or self._closed:
            return
        urgent = str(priority or "").strip().lower() == "urgent"
        existing = self._tasks.get(key)
        if existing is not None:
            if urgent and not self._priorities.get(key, False) and key not in self._running and not existing.done():
                existing.cancel("promoted to urgent")
                self._superseded.add(existing)
                self._priorities[key] = True
                self._tasks[key] = self._create_task(key, urgent=True)
            return
        self._priorities[key] = urgent
        self._tasks[key] = self._create_task(key, urgent=urgent)

    def _create_task(self, content_id: str, *, urgent: bool) -> asyncio.Task[Any]:
        task = asyncio.create_task(
            self._run(content_id, urgent=urgent),
            name=f"pycat-archive-summary-{content_id[:32]}",
        )
        return task

    async def wait(self, content_id: str) -> Any:
        task = self._tasks.get(str(content_id or "").strip())
        if task is None:
            return None
        try:
            while not task.done():
                if self._cancel_requested():
                    task.cancel("run cancelled")
                    break
                await asyncio.wait((task,), timeout=0.1)
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                return None
            raise
        except Exception:
            return None

    async def drain(self) -> None:
        tasks = self._all_tasks()
        pending = {task for task in tasks if not task.done()}
        while pending:
            if self._cancel_requested():
                for task in pending:
                    task.cancel("run cancelled")
                break
            _, pending = await asyncio.wait(pending, timeout=0.1)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._superseded.clear()

    async def cancel_all(self, reason: str = "") -> None:
        self._closed = True
        tasks = self._all_tasks()
        for task in tasks:
            if not task.done():
                task.cancel(str(reason or "run finished"))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._superseded.clear()

    async def _run(self, content_id: str, *, urgent: bool) -> Any:
        try:
            if urgent:
                async with self._total_slots:
                    return await self._summarize_running(content_id)
            async with self._background_slots:
                async with self._total_slots:
                    return await self._summarize_running(content_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Archive summary failed for %s: %s", content_id, exc)
            return None

    async def _summarize_running(self, content_id: str) -> Any:
        self._running.add(content_id)
        try:
            return await self._summarize(content_id)
        finally:
            self._running.discard(content_id)

    def _all_tasks(self) -> tuple[asyncio.Task[Any], ...]:
        return tuple({*self._tasks.values(), *self._superseded})

    def _cancel_requested(self) -> bool:
        try:
            return bool(self._cancel_event and self._cancel_event.is_set())
        except Exception:
            return False
