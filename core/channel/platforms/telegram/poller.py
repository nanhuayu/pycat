from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

from core.channel.connection import ChannelConnectionSnapshot, ChannelConnectionState, ChannelRequiredAction
from core.channel.host import ChannelHost
from core.channel.platforms.telegram.client import TelegramChannelClient
from core.channel.platforms.telegram.delivery import TelegramDelivery
from core.channel.platforms.telegram.router import normalize_telegram_update
from models.contracts.channel import ChannelConfig


logger = logging.getLogger(__name__)


@dataclass
class TelegramPollerHandle:
    channel_id: str
    stop_event: threading.Event
    thread: threading.Thread | None = None
    next_offset: int = 0
    state: ChannelConnectionState = ChannelConnectionState.CONNECTING

    def stop(self) -> None:
        stop_telegram_poller(self)
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=3.0)


def start_telegram_poller(
    context: ChannelHost,
    channel: ChannelConfig,
    *,
    delivery: TelegramDelivery,
) -> TelegramPollerHandle | None:
    channel_id = str(getattr(channel, "id", "") or "").strip()
    if not channel_id:
        return None

    config = dict(getattr(channel, "config", {}) or {})
    if not str(config.get("bot_token", "") or config.get("token", "") or "").strip():
        logger.warning("Skipped Telegram poller for channel %s because Bot Token is incomplete", channel_id)
        context.report_connection(
            ChannelConnectionSnapshot(
                channel_id=channel_id,
                channel_type="telegram",
                mode="polling",
                state=ChannelConnectionState.INCOMPLETE,
                required_action=ChannelRequiredAction.RETRY,
                detail="Telegram 缺少 Bot Token。",
            )
        )
        return None

    handle = TelegramPollerHandle(channel_id=channel_id, stop_event=threading.Event())
    thread = threading.Thread(
        target=run_telegram_poller_loop,
        args=(context, handle),
        name=f"PyCat-TelegramPoller-{channel_id}",
        daemon=True,
    )
    handle.thread = thread
    thread.start()
    logger.info("Started Telegram poller for channel %s", channel_id)
    return handle


def stop_telegram_poller(handle: TelegramPollerHandle) -> None:
    handle.stop_event.set()


def run_telegram_poller_loop(
    context: ChannelHost,
    handle: TelegramPollerHandle,
    *,
    client: TelegramChannelClient | None = None,
) -> None:
    telegram_client = client or TelegramChannelClient()
    retry_delay = 1.0

    while not handle.stop_event.is_set():
        if context.is_stopping():
            break

        channel = context.get_channel(handle.channel_id)
        if channel is None:
            break

        try:
            updates = telegram_client.get_updates(channel, offset=handle.next_offset)
            if handle.stop_event.is_set() or context.is_stopping():
                break

            if handle.state != ChannelConnectionState.READY:
                _report_status(context, handle, channel, state=ChannelConnectionState.READY, detail="Telegram Bot 长轮询已启动，无需公网回调。")

            for raw_update in updates:
                if handle.stop_event.is_set() or context.is_stopping():
                    break
                update_id = _read_update_id(raw_update)
                if update_id is not None:
                    handle.next_offset = max(handle.next_offset, update_id + 1)
                envelope = normalize_telegram_update(
                    raw_update,
                    mark_recent=lambda message_id: context.mark_recent_message(channel.id, message_id),
                )
                if envelope is None or envelope.kind != "message":
                    continue
                context.enqueue_channel_message(channel, envelope.content, meta=envelope.meta)
            retry_delay = 1.0
        except Exception as exc:
            if handle.stop_event.is_set() or context.is_stopping():
                break
            logger.warning("Telegram poller failed for channel %s: %s", handle.channel_id, exc)
            _report_status(context, handle, channel, state=ChannelConnectionState.RECONNECTING, detail=f"Telegram 长轮询异常：{exc}")
            handle.stop_event.wait(retry_delay)
            retry_delay = min(retry_delay * 2.0, 30.0)

    logger.info("Stopped Telegram poller for channel %s", handle.channel_id)


def _report_status(
    context: ChannelHost,
    handle: TelegramPollerHandle,
    channel: ChannelConfig,
    *,
    state: ChannelConnectionState,
    detail: str,
) -> None:
    handle.state = state
    context.report_connection(
        ChannelConnectionSnapshot(
            channel_id=handle.channel_id,
            channel_type="telegram",
            mode="polling",
            state=state,
            detail=detail,
        )
    )


def _read_update_id(raw_update: Any) -> int | None:
    if not isinstance(raw_update, dict):
        return None
    try:
        return int(raw_update.get("update_id"))
    except Exception:
        return None


__all__ = ["TelegramPollerHandle", "run_telegram_poller_loop", "start_telegram_poller", "stop_telegram_poller"]
