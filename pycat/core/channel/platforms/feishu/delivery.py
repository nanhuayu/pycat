from __future__ import annotations

from typing import Any

from pycat.core.channel.envelope import channel_value
from pycat.core.channel.media import ChannelMediaTransfer
from pycat.core.channel.platforms.feishu.client import FeishuChannelClient
from pycat.models.contracts.channel import ChannelConfig
from pycat.models.conversation import Message


class FeishuDelivery:
    """Feishu-specific reply delivery and inbound binding helpers."""

    def __init__(self, client: FeishuChannelClient | None = None, *, content_resolver=None) -> None:
        self._client = client or FeishuChannelClient()
        self._media = ChannelMediaTransfer(content_resolver)

    def process_message(self, context: Any, channel: ChannelConfig, message: Message) -> None:
        user_id = channel_value(message, "user")
        chat_id = channel_value(message, "chat_id") or channel_value(message, "thread_id")
        reply_user = (
            channel_value(message, "reply_user")
            or str((channel.config or {}).get("chat_id", "") or "").strip()
            or chat_id
            or user_id
        )
        thread_id = channel_value(message, "thread_id") or chat_id or user_id

        def _send_reply(content: str, _source_message: Message | None = None) -> None:
            self.send_reply(channel, receive_id=reply_user, content=content)

        self._media.process(
            context,
            channel,
            message,
            download=lambda item, folder: self._client.download_media(channel, item, folder),
            send_file=lambda file: self._client.send_file(channel, receive_id=reply_user, file=file),
            binding_key=thread_id or reply_user or user_id,
            user_id=user_id,
            thread_id=thread_id,
            reply_user=reply_user,
            context_token="",
            platform_label="Feishu",
            reply_normalizer=self._client.normalize_reply_text,
            reply_sender=_send_reply,
        )

    def send_reply(self, channel: ChannelConfig, *, receive_id: str, content: str) -> None:
        self._client.send_text_message(channel, receive_id=receive_id, text=content, receive_id_type="chat_id")

    def normalize_reply_text(self, content: Any, *, limit: int = 4000) -> str:
        return self._client.normalize_reply_text(content, limit=limit)


__all__ = ["FeishuDelivery"]
