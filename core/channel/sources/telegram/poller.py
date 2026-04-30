from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

from core.channel.sources.context import ChannelRuntimeContext
from core.channel.sources.telegram.client import TelegramChannelClient
from core.channel.sources.telegram.router import normalize_telegram_update
from core.config.schema import ChannelConfig


logger = logging.getLogger(__name__)


@dataclass
class TelegramPollerHandle:
    channel_id: str
    stop_event: threading.Event
    thread: threading.Thread | None = None
    next_offset: int = 0
    status: str = "connecting"
    status_detail: str = ""


def start_telegram_poller(context: ChannelRuntimeContext, channel: ChannelConfig) -> TelegramPollerHandle | None:
    channel_id = str(getattr(channel, "id", "") or "").strip()
    if not channel_id:
        return None

    existing = context.get_telegram_poller_worker(channel_id)
    if existing and existing.thread is not None and existing.thread.is_alive():
        return existing

    config = dict(getattr(channel, "config", {}) or {})
    if not str(config.get("bot_token", "") or config.get("token", "") or "").strip():
        logger.warning("Skipped Telegram poller for channel %s because Bot Token is incomplete", channel_id)
        return None

    handle = TelegramPollerHandle(channel_id=channel_id, stop_event=threading.Event())
    thread = threading.Thread(
        target=run_telegram_poller_loop,
        args=(context, handle),
        name=f"PyCat-TelegramPoller-{channel_id}",
        daemon=True,
    )
    handle.thread = thread
    context.remember_telegram_poller_worker(channel_id, handle)
    thread.start()
    logger.info("Started Telegram poller for channel %s", channel_id)
    return handle


def stop_telegram_poller(handle: TelegramPollerHandle) -> None:
    handle.stop_event.set()


def run_telegram_poller_loop(
    context: ChannelRuntimeContext,
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

            if handle.status != "ready":
                _report_status(context, handle, channel, status="ready", detail="Telegram Bot 长轮询已启动，无需公网回调。")

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
            _report_status(context, handle, channel, status="error", detail=f"Telegram 长轮询异常：{exc}")
            handle.stop_event.wait(retry_delay)
            retry_delay = min(retry_delay * 2.0, 30.0)

    logger.info("Stopped Telegram poller for channel %s", handle.channel_id)


def _report_status(
    context: ChannelRuntimeContext,
    handle: TelegramPollerHandle,
    channel: ChannelConfig,
    *,
    status: str,
    detail: str,
) -> None:
    handle.status = status
    handle.status_detail = detail
    try:
        updated = context.update_channel_runtime_state(
            channel,
            config_updates={"status_detail": detail},
            status=status,
        )
        context.remember_channel(updated)
    except Exception as exc:
        logger.debug("Failed to persist Telegram poller state for %s: %s", handle.channel_id, exc)


def _read_update_id(raw_update: Any) -> int | None:
    if not isinstance(raw_update, dict):
        return None
    try:
        return int(raw_update.get("update_id"))
    except Exception:
        return None


__all__ = ["TelegramPollerHandle", "run_telegram_poller_loop", "start_telegram_poller", "stop_telegram_poller"]