from __future__ import annotations

from pycat.core.channel.connection import ChannelConnectionSnapshot, ChannelConnectionState
from pycat.core.channel.host import ChannelHost
from pycat.core.channel.platforms.base import ChannelPlatformBackend
from pycat.core.channel.platforms.qqbot.client import QQBOT_OPEN_BASE
from pycat.core.channel.platforms.qqbot.delivery import QQBotDelivery
from pycat.core.channel.platforms.qqbot.webhook_server import start_qqbot_webhook_server
from pycat.core.channel.platforms.qqbot.ws_client import start_qqbot_ws_client
from pycat.models.contracts.channel import ChannelConfig
from pycat.models.conversation import Conversation, Message


def resolve_qqbot_connection_mode(channel: ChannelConfig) -> str:
    config = dict(getattr(channel, "config", {}) or {})
    mode = str(config.get("connection_mode", "websocket") or "websocket").strip().lower() or "websocket"
    if mode not in {"webhook", "websocket"}:
        mode = "websocket"
    return mode


class QQBotChannelPlatformBackend(ChannelPlatformBackend):
    channel_type = "qqbot"

    def __init__(self, delivery: QQBotDelivery | None = None) -> None:
        self._delivery = delivery or QQBotDelivery()

    def start(self, context: ChannelHost, channel: ChannelConfig) -> None:
        mode = resolve_qqbot_connection_mode(channel)
        if mode == "webhook":
            handle = start_qqbot_webhook_server(context, channel)
            if handle is not None:
                context.remember_connection_handle(handle)
                context.report_connection(
                    ChannelConnectionSnapshot(
                        channel_id=channel.id,
                        channel_type="qqbot",
                        mode="webhook",
                        state=ChannelConnectionState.READY,
                        detail="QQ Bot Webhook 已启动。",
                    )
                )
            else:
                context.report_connection(
                    ChannelConnectionSnapshot(
                        channel_id=channel.id,
                        channel_type="qqbot",
                        mode="webhook",
                        state=ChannelConnectionState.ERROR,
                        detail="QQ Bot Webhook 启动失败，请检查监听地址和端口。",
                    )
                )
            return
        handle = start_qqbot_ws_client(context, channel)
        if handle is not None:
            context.remember_connection_handle(handle)
        else:
            context.report_connection(self.connection_snapshot(context, channel))

    def connection_snapshot(self, context: ChannelHost, channel: ChannelConfig) -> ChannelConnectionSnapshot:
        config = dict(getattr(channel, "config", {}) or {})
        mode = resolve_qqbot_connection_mode(channel)
        host = str(config.get("listen_host", "127.0.0.1") or "127.0.0.1").strip() or "127.0.0.1"
        port = str(config.get("listen_port", "18965") or "18965").strip() or "18965"
        path = str(config.get("callback_path", f"/qqbot/{getattr(channel, 'id', '')}") or f"/qqbot/{getattr(channel, 'id', '')}").strip() or f"/qqbot/{getattr(channel, 'id', '')}"
        if not channel.enabled:
            state = ChannelConnectionState.DISABLED
            detail = "频道已停用。"
        elif not all(str(config.get(key, "") or "").strip() for key in ("app_id", "app_secret")):
            state = ChannelConnectionState.INCOMPLETE
            detail = "缺少 App ID 或 App Secret。"
        elif mode == "websocket":
            state = ChannelConnectionState.CONNECTING
            detail = "QQ Bot Gateway 将在应用设置后启动。"
        else:
            state = ChannelConnectionState.CONNECTING
            detail = f"当前使用 QQ Bot webhook 模式：{host}:{port}{path}"
            detail += "；回发目标会从 QQ 入站事件自动识别。"
        return ChannelConnectionSnapshot(
            channel_id=str(getattr(channel, "id", "") or ""),
            channel_type="qqbot",
            mode=mode,
            state=state,
            detail=detail,
            raw={"api_base_url": QQBOT_OPEN_BASE, **config},
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
        settings = getattr(conversation, "settings", {}) or {}
        binding = settings.get("channel_binding") if isinstance(settings, dict) else None
        target_type = str((binding or {}).get("target_type", "") if isinstance(binding, dict) else "").strip()
        receive_id = str(reply_user or "").strip() or str((channel.config or {}).get("target_id", "") or "").strip()
        if not receive_id:
            raise RuntimeError("当前 QQ Bot 频道会话还没有可用目标，无法回发消息。")
        self._delivery.send_reply(channel, receive_id=receive_id, content=text, context_token=context_token, target_type=target_type)
        return True


__all__ = ["QQBotChannelPlatformBackend", "resolve_qqbot_connection_mode"]
