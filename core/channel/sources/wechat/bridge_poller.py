from __future__ import annotations

import logging
import threading
import time

from core.channel.models import _WeChatBridgePollerHandle
from core.channel.sources.context import ChannelRuntimeContext
from core.config.schema import ChannelConfig


logger = logging.getLogger(__name__)


def start_wechat_bridge_worker(context: ChannelRuntimeContext, channel: ChannelConfig) -> _WeChatBridgePollerHandle | None:
    channel_id = str(getattr(channel, "id", "") or "").strip()
    if not channel_id:
        return None
    if context.resolve_wechat_bridge_credentials(channel) is None:
        return None

    existing = context.get_wechat_bridge_worker(channel_id)
    if existing and existing.thread is not None and existing.thread.is_alive():
        return existing

    handle = _WeChatBridgePollerHandle(
        channel_id=channel_id,
        stop_event=threading.Event(),
        uin=context.get_wechat_bridge_uin(channel_id),
    )
    thread = threading.Thread(
        target=run_wechat_bridge_worker_loop,
        args=(context, handle),
        name=f"PyCat-WeChatBridge-{channel_id}",
        daemon=True,
    )
    handle.thread = thread
    context.remember_wechat_bridge_worker(channel_id, handle)
    thread.start()
    logger.info("Started WeChat QR bridge worker for channel %s", channel_id)
    return handle


def run_wechat_bridge_worker_loop(context: ChannelRuntimeContext, handle: _WeChatBridgePollerHandle) -> None:
    cursor = ""
    retry_delay = 1.0

    while not handle.stop_event.is_set():
        if context.is_stopping():
            break

        channel = context.get_channel(handle.channel_id)
        if channel is None:
            break

        credentials = context.resolve_wechat_bridge_credentials(channel)
        if credentials is None:
            time.sleep(1.0)
            continue

        try:
            payload = context.fetch_wechat_bridge_updates(
                base_url=credentials["base_url"],
                bot_token=credentials["bot_token"],
                uin=handle.uin,
                cursor=cursor,
            )
            if handle.stop_event.is_set() or context.is_stopping():
                break

            next_cursor = context.coalesce_text(payload, "get_updates_buf", default=cursor)
            if next_cursor:
                cursor = next_cursor

            raw_messages = payload.get("msgs")
            if isinstance(raw_messages, list):
                for raw_message in raw_messages:
                    if handle.stop_event.is_set() or context.is_stopping():
                        break
                    context.enqueue_wechat_bridge_message(channel, raw_message)

            retry_delay = 1.0
        except Exception as exc:
            if handle.stop_event.is_set() or context.is_stopping():
                break

            if context.is_wechat_bridge_reauth_required(exc):
                logger.warning(
                    "WeChat QR bridge session expired for channel %s: %s",
                    handle.channel_id,
                    exc,
                )
                context.update_channel_runtime_state(
                    channel,
                    config_updates={
                        "bridge_bot_token": "",
                        "bridge_bot_id": "",
                        "bridge_user_id": "",
                        "qr_status": "expired",
                        "status_detail": "扫码登录已失效，请重新生成二维码并扫码。",
                    },
                    status="draft",
                )
                break

            if context.is_wechat_bridge_transient_poll_error(exc):
                logger.debug(
                    "WeChat QR bridge polling transient error for channel %s: %s",
                    handle.channel_id,
                    exc,
                )
                time.sleep(min(retry_delay, 3.0))
                retry_delay = min(retry_delay * 1.5, 6.0)
                continue

            logger.warning(
                "WeChat QR bridge polling failed for channel %s: %s",
                handle.channel_id,
                exc,
            )
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2.0, 10.0)

    logger.info("Stopped WeChat QR bridge worker for channel %s", handle.channel_id)


__all__ = ["run_wechat_bridge_worker_loop", "start_wechat_bridge_worker"]