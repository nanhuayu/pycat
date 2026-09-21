from __future__ import annotations

import logging
import threading

from pycat.core.channel.connection import (
    ChannelConnectionHandle,
    ChannelConnectionSnapshot,
    ChannelConnectionState,
    ChannelRequiredAction,
)
from pycat.core.channel.host import ChannelHost
from pycat.core.channel.platforms.base import ChannelPlatformBackend
from pycat.core.channel.queue import ChannelQueue
from pycat.core.channel.scheduler import ChannelTurnScheduler
from pycat.core.channel.worker import ChannelWorker
from pycat.models.contracts.channel import ChannelConfig


logger = logging.getLogger(__name__)


class ChannelLifecycle:
    """Owns platform backend registration and gateway worker lifecycle."""

    def __init__(
        self,
        *,
        queue: ChannelQueue,
        worker: ChannelWorker,
        scheduler: ChannelTurnScheduler,
        channel_host: ChannelHost,
        clear_channels,
    ) -> None:
        self._queue = queue
        self._worker = worker
        self._scheduler = scheduler
        self._channel_host = channel_host
        self._clear_channels = clear_channels
        self._backends: dict[str, ChannelPlatformBackend] = {}
        self._handles: dict[str, ChannelConnectionHandle] = {}
        self._snapshots: dict[str, ChannelConnectionSnapshot] = {}
        self._lock = threading.Lock()

    def register_backend(self, backend: ChannelPlatformBackend) -> None:
        channel_type = str(getattr(backend, "channel_type", "") or "").strip().lower()
        if not channel_type:
            return
        self._backends[channel_type] = backend

    def get_backend(self, channel: ChannelConfig) -> ChannelPlatformBackend | None:
        channel_type = str(getattr(channel, "type", "") or "").strip().lower()
        if not channel_type:
            return None
        return self._backends.get(channel_type)

    def start(self, channels: list[ChannelConfig]) -> None:
        if not channels:
            return
        self._scheduler.start()
        self._worker.start()
        for channel in channels:
            backend = self.get_backend(channel)
            if backend is None:
                self.report_connection(
                    ChannelConnectionSnapshot(
                        channel_id=channel.id,
                        channel_type=channel.type,
                        state=ChannelConnectionState.INCOMPLETE,
                        required_action=ChannelRequiredAction.RETRY,
                        detail="当前频道类型没有可用连接实现。",
                    )
                )
                continue
            self.report_connection(
                ChannelConnectionSnapshot(
                    channel_id=channel.id,
                    channel_type=channel.type,
                    mode=str((channel.config or {}).get("connection_mode", "") or ""),
                    state=ChannelConnectionState.CONNECTING,
                    detail="正在启动频道连接。",
                )
            )
            try:
                backend.start(self._channel_host, channel)
            except Exception as exc:
                logger.warning("Failed to start channel %s: %s", channel.id, exc)
                self.report_connection(
                    ChannelConnectionSnapshot(
                        channel_id=channel.id,
                        channel_type=channel.type,
                        mode=str((channel.config or {}).get("connection_mode", "") or ""),
                        state=ChannelConnectionState.ERROR,
                        required_action=ChannelRequiredAction.RETRY,
                        detail=f"频道连接启动失败：{exc}",
                    )
                )

    def stop(self) -> None:
        with self._lock:
            handles = list(self._handles.values())
            self._handles.clear()
        for handle in handles:
            try:
                handle.stop()
            except Exception as exc:
                logger.debug("Failed to stop channel connection %s: %s", getattr(handle, "channel_id", ""), exc)

        self._worker.stop(timeout=2.0)
        self._scheduler.stop(wait=False)
        self._queue.clear()
        self._clear_channels()
        with self._lock:
            self._snapshots.clear()

    def remember_connection_handle(self, handle: ChannelConnectionHandle) -> None:
        channel_id = str(getattr(handle, "channel_id", "") or "").strip()
        if not channel_id:
            return
        with self._lock:
            existing = self._handles.get(channel_id)
            self._handles[channel_id] = handle
        if existing is not None and existing is not handle:
            try:
                existing.stop()
            except Exception as exc:
                logger.debug("Failed to replace channel connection %s: %s", channel_id, exc)

    def report_connection(self, snapshot: ChannelConnectionSnapshot) -> None:
        channel_id = str(snapshot.channel_id or "").strip()
        if not channel_id:
            return
        with self._lock:
            self._snapshots[channel_id] = snapshot

    def connection_snapshot(self, channel_id: str) -> ChannelConnectionSnapshot | None:
        with self._lock:
            return self._snapshots.get(str(channel_id or "").strip())


__all__ = ["ChannelLifecycle"]
