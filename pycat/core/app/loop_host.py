"""One application-lifetime asyncio host for synchronous desktop adapters."""

from __future__ import annotations

import asyncio
import threading


class ApplicationLoop:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._closing = False
        self._thread = threading.Thread(target=self._serve, name="pycat-application", daemon=True)
        self._thread.start()
        self._ready.wait()

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._ready.set)
        try:
            self.loop.run_forever()
        finally:
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.run_until_complete(self.loop.shutdown_default_executor())
            self.loop.close()

    def submit(self, coroutine):
        if not self._thread.is_alive():
            coroutine.close()
            raise RuntimeError("Application event loop is closed.")
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    def close(self):
        if self._thread.is_alive() and not self._closing:
            self._closing = True
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                raise RuntimeError("Application event loop did not stop.")
