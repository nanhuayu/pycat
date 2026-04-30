from __future__ import annotations

from core.channel.sources.base import ChannelRuntimeBackend
from core.channel.models import ChannelConnectionSnapshot
from core.channel.sources.context import ChannelRuntimeContext
from core.channel.sources.feishu.runtime_service import FeishuRuntimeSource
from core.config.schema import ChannelConfig
from models.conversation import Conversation, Message


def resolve_feishu_connection_mode(channel: ChannelConfig) -> str:
    config = dict(getattr(channel, "config", {}) or {})
    mode = str(config.get("connection_mode", "websocket") or "websocket").strip().lower() or "websocket"
    if mode not in {"webhook", "websocket"}:
        mode = "websocket"
    if mode == "webhook" and not str(config.get("verification_token", "") or "").strip():
        return "websocket"
    return mode


class FeishuChannelBackend(ChannelRuntimeBackend):
    channel_type = "feishu"

    def __init__(self, runtime_source: FeishuRuntimeSource | None = None) -> None:
        self._runtime_source = runtime_source or FeishuRuntimeSource()

    def start(self, context: ChannelRuntimeContext, channel: ChannelConfig) -> None:
        mode = resolve_feishu_connection_mode(channel)
        if mode == "webhook":
            context.start_feishu_webhook_server(channel)
            return
        context.start_feishu_websocket_client(channel)

    def connection_snapshot(self, context: ChannelRuntimeContext, channel: ChannelConfig) -> ChannelConnectionSnapshot:
        config = dict(getattr(channel, "config", {}) or {})
        mode = resolve_feishu_connection_mode(channel)
        host = str(config.get("listen_host", "127.0.0.1") or "127.0.0.1").strip() or "127.0.0.1"
        port = str(config.get("listen_port", "18964") or "18964").strip() or "18964"
        path = str(config.get("callback_path", f"/feishu/{getattr(channel, 'id', '')}") or f"/feishu/{getattr(channel, 'id', '')}").strip() or f"/feishu/{getattr(channel, 'id', '')}"
        if mode == "websocket":
            detail = "当前使用飞书长连接模式：使用 App ID / App Secret 直连，无需公网回调。"
            configured_mode = str(config.get("connection_mode", "websocket") or "websocket").strip().lower() or "websocket"
            if configured_mode == "webhook" and not str(config.get("verification_token", "") or "").strip():
                detail += " 已检测到 webhook token 缺失，已自动回退为长连接模式。"
        else:
            detail = f"当前使用飞书 webhook 模式：{host}:{port}{path}"
            if not str(config.get("verification_token", "") or "").strip():
                detail += "（尚未填写 Verification Token）"
        return ChannelConnectionSnapshot(
            channel_id=str(getattr(channel, "id", "") or ""),
            channel_type="feishu",
            mode=mode,
            status=str(getattr(channel, "status", "draft") or "draft"),
            detail=detail,
            raw=config,
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
        receive_id = str(reply_user or "").strip() or str((channel.config or {}).get("chat_id", "") or "").strip()
        if not receive_id:
            raise RuntimeError("当前飞书频道会话还没有可用 chat_id，无法回发消息。")
        self._runtime_source.send_reply(channel, receive_id=receive_id, content=text)
        return True


__all__ = ["FeishuChannelBackend", "resolve_feishu_connection_mode"]