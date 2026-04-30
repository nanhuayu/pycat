from __future__ import annotations

from typing import Any

from core.channel.sources.base import ChannelRuntimeBackend
from core.channel.models import ChannelConnectionSnapshot
from core.channel.sources.context import ChannelRuntimeContext
from core.channel.sources.wechat.client import WECHAT_CHERRY_BRIDGE_BASE
from core.channel.sources.wechat.runtime_service import WeChatRuntimeSource
from core.config.schema import ChannelConfig
from models.conversation import Conversation, Message


class WeChatChannelBackend(ChannelRuntimeBackend):
    channel_type = "wechat"

    def __init__(self, runtime_source: WeChatRuntimeSource | None = None) -> None:
        self._runtime_source = runtime_source or WeChatRuntimeSource()

    def start(self, context: ChannelRuntimeContext, channel: ChannelConfig) -> None:
        config = dict(getattr(channel, "config", {}) or {})
        mode = str(config.get("connection_mode", "official-webhook") or "official-webhook").strip().lower() or "official-webhook"
        if mode == "qr-bridge":
            context.start_wechat_bridge_worker(channel)
            return
        context.start_wechat_webhook_server(channel)

    def connection_snapshot(self, context: ChannelRuntimeContext, channel: ChannelConfig) -> ChannelConnectionSnapshot:
        config = dict(getattr(channel, "config", {}) or {})
        mode = str(config.get("connection_mode", "official-webhook") or "official-webhook").strip().lower() or "official-webhook"
        if mode != "qr-bridge":
            return ChannelConnectionSnapshot(
                channel_id=str(getattr(channel, "id", "") or ""),
                channel_type="wechat",
                mode=mode,
                status=str(getattr(channel, "status", "draft") or "draft"),
                detail="当前使用公众号 webhook 模式。",
                raw=config,
            )
        return context.build_wechat_qr_snapshot_from_config(channel)

    def refresh_connection(self, context: ChannelRuntimeContext, channel: ChannelConfig, *, force_new: bool = False) -> ChannelConnectionSnapshot:
        config = dict(getattr(channel, "config", {}) or {})
        mode = str(config.get("connection_mode", "official-webhook") or "official-webhook").strip().lower() or "official-webhook"
        if mode != "qr-bridge":
            return self.connection_snapshot(context, channel)

        bridge_api_base = context.normalize_wechat_bridge_base_url(config.get("bridge_api_base", WECHAT_CHERRY_BRIDGE_BASE))
        session_id = str(config.get("bridge_session_id", "") or "").strip()
        if force_new or not session_id:
            payload = context.create_wechat_qr_bridge_session(channel, bridge_api_base=bridge_api_base)
        else:
            payload = context.fetch_wechat_qr_bridge_session(
                channel,
                bridge_api_base=bridge_api_base,
                session_id=session_id,
            )
        return context.snapshot_from_wechat_qr_payload(channel, payload, fallback=config)

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
        if not reply_user:
            raise RuntimeError("当前微信频道会话还没有最近活跃联系人，无法回发消息。")
        self._runtime_source.send_reply(channel, touser=reply_user, content=text, context_token=context_token)
        return True


__all__ = ["WeChatChannelBackend"]