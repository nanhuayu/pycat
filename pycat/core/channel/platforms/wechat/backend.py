from __future__ import annotations

from pycat.core.channel.connection import (
    ChannelConnectionSnapshot,
    ChannelConnectionState,
    ChannelRequiredAction,
)
from pycat.core.channel.host import ChannelHost
from pycat.core.channel.platforms.base import ChannelPlatformBackend
from pycat.core.channel.platforms.wechat.delivery import WeChatDelivery
from pycat.core.channel.platforms.wechat.polling_transport import start_wechat_polling
from pycat.core.channel.platforms.wechat.webhook_server import start_wechat_webhook_server
from pycat.models.contracts.channel import ChannelConfig
from pycat.models.conversation import Conversation, Message


def resolve_wechat_connection_mode(channel: ChannelConfig) -> str:
    mode = str((channel.config or {}).get("connection_mode", "ilink") or "ilink").strip().lower()
    return mode if mode in {"ilink", "webhook"} else "ilink"


class WeChatChannelPlatformBackend(ChannelPlatformBackend):
    channel_type = "wechat"

    def __init__(self, delivery: WeChatDelivery | None = None) -> None:
        self._delivery = delivery or WeChatDelivery()

    def start(self, context: ChannelHost, channel: ChannelConfig) -> None:
        mode = resolve_wechat_connection_mode(channel)
        if mode == "ilink":
            handle = start_wechat_polling(context, channel, delivery=self._delivery)
        else:
            handle = start_wechat_webhook_server(context, channel)
            if handle is not None:
                context.report_connection(
                    ChannelConnectionSnapshot(
                        channel_id=channel.id,
                        channel_type="wechat",
                        mode="webhook",
                        state=ChannelConnectionState.READY,
                        detail="微信公众号 Webhook 已启动。",
                    )
                )
            else:
                context.report_connection(
                    ChannelConnectionSnapshot(
                        channel_id=channel.id,
                        channel_type="wechat",
                        mode="webhook",
                        state=ChannelConnectionState.ERROR,
                        required_action=ChannelRequiredAction.RETRY,
                        detail="微信公众号 Webhook 启动失败，请检查监听地址和端口。",
                    )
                )
        if handle is not None:
            context.remember_connection_handle(handle)

    def connection_snapshot(self, context: ChannelHost, channel: ChannelConfig) -> ChannelConnectionSnapshot:
        mode = resolve_wechat_connection_mode(channel)
        config = dict(channel.config or {})
        if not channel.enabled:
            state = ChannelConnectionState.DISABLED
            detail = "频道已停用。"
        elif mode == "ilink" and self._delivery.client.resolve_ilink_credentials(channel) is None:
            state = ChannelConnectionState.INCOMPLETE
            detail = "请先完成个人微信扫码连接。"
        elif mode == "webhook" and not all(str(config.get(key, "") or "").strip() for key in ("app_id", "app_secret", "token")):
            state = ChannelConnectionState.INCOMPLETE
            detail = "微信公众号 Webhook 配置不完整。"
        else:
            state = ChannelConnectionState.CONNECTING
            detail = "连接将在应用设置后启动。"
        return ChannelConnectionSnapshot(
            channel_id=channel.id,
            channel_type="wechat",
            mode=mode,
            state=state,
            required_action=(
                ChannelRequiredAction.SCAN
                if mode == "ilink" and state == ChannelConnectionState.INCOMPLETE
                else ChannelRequiredAction.NONE
            ),
            detail=detail,
            account_name=str(config.get("ilink_user_id", "") or "").strip(),
        )

    def process_message(self, context: ChannelHost, channel: ChannelConfig, message: Message) -> None:
        self._delivery.process_message(context, channel, message)

    def send_bound_message(
        self,
        context: ChannelHost,
        channel: ChannelConfig,
        conversation: Conversation,
        *,
        text: str,
        reply_user: str,
        context_token: str,
    ) -> bool:
        if not reply_user:
            raise RuntimeError("当前微信对话还没有最近活跃联系人，无法回发消息。")
        self._delivery.send_reply(channel, touser=reply_user, content=text, context_token=context_token)
        return True


__all__ = ["WeChatChannelPlatformBackend", "resolve_wechat_connection_mode"]
