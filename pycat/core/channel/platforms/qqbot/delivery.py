from __future__ import annotations

from typing import Any

from pycat.core.channel.envelope import channel_value
from pycat.core.channel.media import ChannelMediaTransfer
from pycat.core.channel.platforms.qqbot.client import QQBotChannelClient
from pycat.models.contracts.channel import ChannelConfig
from pycat.models.conversation import Message


class QQBotDelivery:
    """QQ Bot-specific reply delivery and inbound binding helpers."""

    def __init__(self, client: QQBotChannelClient | None = None, *, content_resolver=None) -> None:
        self._client = client or QQBotChannelClient()
        self._media = ChannelMediaTransfer(content_resolver)

    def process_message(self, context: Any, channel: ChannelConfig, message: Message) -> None:
        user_id = channel_value(message, "user")
        thread_id = channel_value(message, "thread_id") or channel_value(message, "chat_id") or user_id
        reply_user = (
            channel_value(message, "reply_user")
            or str((channel.config or {}).get("target_id", "") or "").strip()
            or thread_id
            or user_id
        )
        context_token = channel_value(message, "context_token") or channel_value(message, "message_id")
        target_type = channel_value(message, "target_type")

        def _send_reply(content: str, _source_message: Message | None = None) -> None:
            self.send_reply(channel, receive_id=reply_user, content=content, context_token=context_token, target_type=target_type)

        self._media.process(
            context,
            channel,
            message,
            download=lambda item, folder: self._client.download_media(channel, item, folder),
            send_file=lambda file: self._client.send_file(channel, receive_id=reply_user, file=file,
                context_token=context_token, target_type=target_type),
            binding_key=thread_id or reply_user or user_id,
            user_id=user_id,
            thread_id=thread_id,
            reply_user=reply_user,
            context_token=context_token,
            platform_label="QQ Bot",
            reply_normalizer=self._client.normalize_reply_text,
            binding_updates={"target_type": target_type} if target_type else None,
            reply_sender=_send_reply,
        )

    def send_reply(
        self,
        channel: ChannelConfig,
        *,
        receive_id: str,
        content: str,
        context_token: str = "",
        target_type: str = "",
    ) -> None:
        if target_type:
            payload = channel.to_dict()
            config = dict(payload.get("config", {}) or {})
            config["target_type"] = str(target_type or "").strip()
            payload["config"] = config
            channel = ChannelConfig.from_dict(payload)
        self._client.send_text_message(channel, receive_id=receive_id, text=content, context_token=context_token)

__all__ = ["QQBotDelivery"]
