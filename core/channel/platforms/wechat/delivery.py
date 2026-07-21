from __future__ import annotations

from typing import Any

from core.channel.envelope import channel_value
from core.channel.platforms.wechat.client import WeChatChannelClient
from core.channel.platforms.wechat.protocol import normalize_wechat_reply_text
from core.channel.platforms.wechat.router import normalize_wechat_ilink_message
from models.contracts.channel import ChannelConfig
from models.conversation import Message


class WeChatDelivery:
    """WeChat reply delivery and normalized inbound binding."""

    def __init__(self, client: WeChatChannelClient | None = None) -> None:
        self._client = client or WeChatChannelClient()

    @property
    def client(self) -> WeChatChannelClient:
        return self._client

    def enqueue_ilink_message(self, context: Any, channel: ChannelConfig, raw_message: Any) -> None:
        envelope = normalize_wechat_ilink_message(
            raw_message,
            mark_recent=lambda message_id: context.mark_recent_message(channel.id, message_id),
        )
        if envelope is not None:
            context.enqueue_channel_message(channel, envelope.content, meta=envelope.meta)

    def process_message(self, context: Any, channel: ChannelConfig, message: Message) -> None:
        user_id = channel_value(message, "user") or channel_value(message, "thread_id")
        reply_user = (
            channel_value(message, "reply_user")
            or str((channel.config or {}).get("receiver_id", "") or "").strip()
            or user_id
        )
        context_token = channel_value(message, "context_token")
        thread_id = channel_value(message, "thread_id") or user_id

        def _send_reply(content: str, _source_message: Message | None = None) -> None:
            self.send_reply(
                channel,
                touser=reply_user,
                content=content,
                context_token=context_token,
            )

        context.process_bound_channel_message(
            channel,
            message,
            binding_key=user_id or thread_id,
            user_id=user_id,
            thread_id=thread_id,
            reply_user=reply_user,
            context_token=context_token,
            platform_label="WeChat",
            reply_normalizer=normalize_wechat_reply_text,
            reply_sender=_send_reply,
        )

    def send_reply(self, channel: ChannelConfig, *, touser: str, content: str, context_token: str = "") -> None:
        mode = str((channel.config or {}).get("connection_mode", "ilink") or "ilink").strip().lower()
        if mode == "ilink":
            self._client.send_ilink_reply(
                channel,
                touser=touser,
                content=content,
                context_token=context_token,
            )
            return

        app_id = str((channel.config or {}).get("app_id", "") or "").strip()
        app_secret = str((channel.config or {}).get("app_secret", "") or "").strip()
        if not (app_id and app_secret and touser):
            raise RuntimeError("微信公众号缺少 app_id / app_secret / touser，无法回发消息。")
        access_token = self._client.get_access_token(channel, app_id=app_id, app_secret=app_secret)
        self._client.send_official_reply(access_token, touser=touser, content=content)


__all__ = ["WeChatDelivery"]
