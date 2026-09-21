from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


def cancel_loop_task(loop: asyncio.AbstractEventLoop | None, task: asyncio.Task | None) -> None:
    """Request cancellation of an asyncio task owned by a worker-thread loop."""
    if loop is None or task is None:
        return
    try:
        loop.call_soon_threadsafe(_cancel_task, task)
    except RuntimeError as exc:
        logger.debug("Failed to cancel async worker task: %s", exc)


def _cancel_task(task: asyncio.Task) -> None:
    if not task.done():
        task.cancel()
