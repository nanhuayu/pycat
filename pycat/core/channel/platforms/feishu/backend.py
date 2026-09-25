from __future__ import annotations

from pycat.core.channel.connection import ChannelConnectionSnapshot, ChannelConnectionState
from pycat.core.channel.host import ChannelHost
from pycat.core.channel.platforms.base import ChannelPlatformBackend
from pycat.core.channel.platforms.feishu.delivery import FeishuDelivery
from pycat.core.channel.platforms.feishu.webhook_server import start_feishu_webhook_server
from pycat.core.channel.platforms.feishu.ws_client import start_feishu_ws_client
from pycat.models.contracts.channel import ChannelConfig
from pycat.models.conversation import Conversation, Message


def resolve_feishu_connection_mode(channel: ChannelConfig) -> str:
    config = dict(getattr(channel, "config", {}) or {})
    mode = str(config.get("connection_mode", "websocket") or "websocket").strip().lower() or "websocket"
    if mode not in {"webhook", "websocket"}:
        mode = "websocket"
    return mode


class FeishuChannelPlatformBackend(ChannelPlatformBackend):
    channel_type = "feishu"

    def __init__(self, delivery: FeishuDelivery | None = None) -> None:
        self._delivery = delivery or FeishuDelivery()

    def start(self, context: ChannelHost, channel: ChannelConfig) -> None:
        mode = resolve_feishu_connection_mode(channel)
        if mode == "webhook":
            handle = start_feishu_webhook_server(context, channel)
            if handle is not None:
                context.remember_connection_handle(handle)
                context.report_connection(
                    ChannelConnectionSnapshot(
                        channel_id=channel.id,
                        channel_type="feishu",
                        mode="webhook",
                        state=ChannelConnectionState.READY,
                        detail="飞书 Webhook 已启动。",
                    )
                )
            else:
                context.report_connection(
                    ChannelConnectionSnapshot(
                        channel_id=channel.id,
                        channel_type="feishu",
                        mode="webhook",
                        state=ChannelConnectionState.ERROR,
                        detail="飞书 Webhook 启动失败，请检查监听地址和端口。",
                    )
                )
            return
        handle = start_feishu_ws_client(context, channel)
        if handle is not None:
            context.remember_connection_handle(handle)
        else:
            context.report_connection(self.connection_snapshot(context, channel))

    def connection_snapshot(self, context: ChannelHost, channel: ChannelConfig) -> ChannelConnectionSnapshot:
        config = dict(getattr(channel, "config", {}) or {})
        mode = resolve_feishu_connection_mode(channel)
        host = str(config.get("listen_host", "127.0.0.1") or "127.0.0.1").strip() or "127.0.0.1"
        port = str(config.get("listen_port", "18964") or "18964").strip() or "18964"
        path = str(config.get("callback_path", f"/feishu/{getattr(channel, 'id', '')}") or f"/feishu/{getattr(channel, 'id', '')}").strip() or f"/feishu/{getattr(channel, 'id', '')}"
        if not channel.enabled:
            state = ChannelConnectionState.DISABLED
            detail = "频道已停用。"
        elif not all(str(config.get(key, "") or "").strip() for key in ("app_id", "app_secret")):
            state = ChannelConnectionState.INCOMPLETE
            detail = "缺少 App ID 或 App Secret。"
        elif mode == "webhook" and not str(config.get("verification_token", "") or "").strip():
            state = ChannelConnectionState.INCOMPLETE
            detail = "飞书 Webhook 缺少 Verification Token。"
        elif mode == "websocket":
            state = ChannelConnectionState.CONNECTING
            detail = "飞书长连接将在应用设置后启动。"
        else:
            state = ChannelConnectionState.CONNECTING
            detail = f"当前使用飞书 webhook 模式：{host}:{port}{path}"
        return ChannelConnectionSnapshot(
            channel_id=str(getattr(channel, "id", "") or ""),
            channel_type="feishu",
            mode=mode,
            state=state,
            detail=detail,
            raw=config,
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
        receive_id = str(reply_user or "").strip() or str((channel.config or {}).get("chat_id", "") or "").strip()
        if not receive_id:
            raise RuntimeError("当前飞书频道会话还没有可用 chat_id，无法回发消息。")
        self._delivery.send_reply(channel, receive_id=receive_id, content=text)
        return True


__all__ = ["FeishuChannelPlatformBackend", "resolve_feishu_connection_mode"]
