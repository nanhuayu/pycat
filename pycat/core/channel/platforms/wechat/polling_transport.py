from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from pycat.core.channel.connection import (
    ChannelConnectionSnapshot,
    ChannelConnectionState,
    ChannelRequiredAction,
)
from pycat.core.channel.host import ChannelHost
from pycat.core.channel.platforms.wechat.delivery import WeChatDelivery
from pycat.models.contracts.channel import ChannelConfig


logger = logging.getLogger(__name__)


@dataclass
class WeChatPollingHandle:
    channel_id: str
    stop_event: threading.Event
    thread: threading.Thread | None = None

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None and self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout=3.0)


def start_wechat_polling(
    context: ChannelHost,
    channel: ChannelConfig,
    *,
    delivery: WeChatDelivery,
) -> WeChatPollingHandle | None:
    credentials = delivery.client.resolve_ilink_credentials(channel)
    if credentials is None:
        context.report_connection(
            ChannelConnectionSnapshot(
                channel_id=channel.id,
                channel_type="wechat",
                mode="ilink",
                state=ChannelConnectionState.INCOMPLETE,
                required_action=ChannelRequiredAction.SCAN,
                detail="请先完成个人微信扫码连接。",
            )
        )
        return None
    handle = WeChatPollingHandle(channel_id=channel.id, stop_event=threading.Event())
    thread = threading.Thread(
        target=_run_wechat_polling,
        args=(context, channel, handle, delivery),
        name=f"PyCat-WeChatPolling-{channel.id}",
        daemon=True,
    )
    handle.thread = thread
    thread.start()
    return handle


def _run_wechat_polling(
    context: ChannelHost,
    channel: ChannelConfig,
    handle: WeChatPollingHandle,
    delivery: WeChatDelivery,
) -> None:
    credentials = delivery.client.resolve_ilink_credentials(channel)
    if credentials is None:
        return
    cursor = ""
    retry_delay = 1.0
    notified = False
    try:
        delivery.client.notify_ilink_start(
            api_base=credentials["api_base"],
            token=credentials["token"],
        )
        notified = True
        context.report_connection(
            ChannelConnectionSnapshot(
                channel_id=channel.id,
                channel_type="wechat",
                mode="ilink",
                state=ChannelConnectionState.READY,
                detail="个人微信消息连接已启动。",
                account_name=credentials.get("user_id", "") or credentials.get("bot_id", ""),
            )
        )
        while not handle.stop_event.is_set() and not context.is_stopping():
            try:
                payload = delivery.client.fetch_ilink_updates(
                    api_base=credentials["api_base"],
                    token=credentials["token"],
                    cursor=cursor,
                )
                if handle.stop_event.is_set() or context.is_stopping():
                    break
                next_cursor = delivery.client.coalesce_text(
                    payload,
                    "get_updates_buf",
                    "cursor",
                    "next_cursor",
                    default=cursor,
                )
                if next_cursor:
                    cursor = next_cursor
                messages = payload.get("msgs") or payload.get("messages") or payload.get("updates")
                if isinstance(messages, list):
                    for raw_message in messages:
                        if handle.stop_event.is_set() or context.is_stopping():
                            break
                        delivery.enqueue_ilink_message(context, channel, raw_message)
                retry_delay = 1.0
            except Exception as exc:
                if handle.stop_event.is_set() or context.is_stopping():
                    break
                if delivery.client.is_reauth_required(exc):
                    context.report_connection(
                        ChannelConnectionSnapshot(
                            channel_id=channel.id,
                            channel_type="wechat",
                            mode="ilink",
                            state=ChannelConnectionState.ERROR,
                            required_action=ChannelRequiredAction.RETRY,
                            detail="个人微信登录已失效，请重新扫码。",
                        )
                    )
                    break
                context.report_connection(
                    ChannelConnectionSnapshot(
                        channel_id=channel.id,
                        channel_type="wechat",
                        mode="ilink",
                        state=ChannelConnectionState.RECONNECTING,
                        detail=f"个人微信消息连接正在重试：{exc}",
                    )
                )
                handle.stop_event.wait(retry_delay)
                retry_delay = min(retry_delay * 2.0, 10.0)
    except Exception as exc:
        logger.warning("Failed to start WeChat polling for channel %s: %s", channel.id, exc)
        context.report_connection(
            ChannelConnectionSnapshot(
                channel_id=channel.id,
                channel_type="wechat",
                mode="ilink",
                state=ChannelConnectionState.ERROR,
                required_action=ChannelRequiredAction.RETRY,
                detail=f"个人微信连接启动失败：{exc}",
            )
        )
    finally:
        if notified:
            try:
                delivery.client.notify_ilink_stop(
                    api_base=credentials["api_base"],
                    token=credentials["token"],
                )
            except Exception as exc:
                logger.debug("Failed to notify WeChat stop for channel %s: %s", channel.id, exc)


__all__ = ["WeChatPollingHandle", "start_wechat_polling"]
