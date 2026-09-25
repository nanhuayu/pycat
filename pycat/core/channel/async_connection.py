from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass
class AsyncConnectionHandle:
    channel_id: str
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    loop: asyncio.AbstractEventLoop | None = None
    websocket: Any = None

    def stop(self) -> None:
        stop_async_connection(self)
        if self.thread is not None and self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout=3.0)


def run_async_connection_thread(
    handle: AsyncConnectionHandle,
    *,
    thread_name: str,
    run: Callable[[], Awaitable[None]],
) -> AsyncConnectionHandle:
    def _target() -> None:
        loop = asyncio.new_event_loop()
        handle.loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(run())
        except Exception as exc:
            if not handle.stop_event.is_set():
                logger.warning("Async channel connection %s crashed: %s", handle.channel_id, exc)
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                try:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                except Exception:
                    pass
            loop.close()
            handle.loop = None
            handle.websocket = None

    handle.thread = threading.Thread(target=_target, name=thread_name, daemon=True)
    handle.thread.start()
    return handle


def stop_async_connection(handle: AsyncConnectionHandle) -> None:
    handle.stop_event.set()
    if handle.loop is not None and handle.websocket is not None:
        try:
            asyncio.run_coroutine_threadsafe(handle.websocket.close(), handle.loop)
        except Exception:
            pass


__all__ = ["AsyncConnectionHandle", "run_async_connection_thread", "stop_async_connection"]
