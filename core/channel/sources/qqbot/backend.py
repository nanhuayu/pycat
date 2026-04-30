from __future__ import annotations

from core.channel.models import ChannelConnectionSnapshot
from core.channel.sources.base import ChannelRuntimeBackend
from core.channel.sources.context import ChannelRuntimeContext
from core.channel.sources.qqbot.client import QQBOT_OPEN_BASE
from core.channel.sources.qqbot.runtime_service import QQBotRuntimeSource
from core.config.schema import ChannelConfig
from models.conversation import Conversation, Message


def resolve_qqbot_connection_mode(channel: ChannelConfig) -> str:
    config = dict(getattr(channel, "config", {}) or {})
    mode = str(config.get("connection_mode", "websocket") or "websocket").strip().lower() or "websocket"
    if mode not in {"webhook", "websocket"}:
        mode = "websocket"
    return mode


class QQBotChannelBackend(ChannelRuntimeBackend):
    channel_type = "qqbot"

    def __init__(self, runtime_source: QQBotRuntimeSource | None = None) -> None:
        self._runtime_source = runtime_source or QQBotRuntimeSource()

    def start(self, context: ChannelRuntimeContext, channel: ChannelConfig) -> None:
        mode = resolve_qqbot_connection_mode(channel)
        if mode == "webhook":
            context.start_qqbot_webhook_server(channel)
            return
        context.start_qqbot_websocket_client(channel)

    def connection_snapshot(self, context: ChannelRuntimeContext, channel: ChannelConfig) -> ChannelConnectionSnapshot:
        config = dict(getattr(channel, "config", {}) or {})
        mode = resolve_qqbot_connection_mode(channel)
        host = str(config.get("listen_host", "127.0.0.1") or "127.0.0.1").strip() or "127.0.0.1"
        port = str(config.get("listen_port", "18965") or "18965").strip() or "18965"
        path = str(config.get("callback_path", f"/qqbot/{getattr(channel, 'id', '')}") or f"/qqbot/{getattr(channel, 'id', '')}").strip() or f"/qqbot/{getattr(channel, 'id', '')}"
        if mode == "websocket":
            detail = "当前使用 QQ Bot 官方 Gateway 长连接：只需 App ID / App Secret，无需公网回调。"
            status_detail = str(config.get("status_detail", "") or "").strip()
            if status_detail:
                detail += f" {status_detail}"
        else:
            detail = f"当前使用 QQ Bot webhook 模式：{host}:{port}{path}"
            detail += "；回发目标会从 QQ 入站事件自动识别。"
        return ChannelConnectionSnapshot(
            channel_id=str(getattr(channel, "id", "") or ""),
            channel_type="qqbot",
            mode=mode,
            status=str(getattr(channel, "status", "draft") or "draft"),
            detail=detail,
            raw={"api_base_url": QQBOT_OPEN_BASE, **config},
        )

    def process_message(self, context: ChannelRuntimeContext, channel: ChannelConfig, message: Message) -> None:
        self._runtime_source.process_message(context, channel, message)

    def send_bound_message(
        self,
        context: ChannelRuntimeContext,
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
        self._runtime_source.send_reply(channel, receive_id=receive_id, content=text, context_token=context_token, target_type=target_type)
        return True


__all__ = ["QQBotChannelBackend", "resolve_qqbot_connection_mode"]