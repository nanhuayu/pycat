from __future__ import annotations

from typing import Any

from pycat.core.channel.envelope import channel_value
from pycat.core.channel.media import ChannelMediaTransfer
from pycat.core.channel.platforms.telegram.client import TelegramChannelClient
from pycat.models.contracts.channel import ChannelConfig
from pycat.models.conversation import Message


class TelegramDelivery:
    """Telegram-specific reply delivery and inbound binding helpers."""

    def __init__(self, client: TelegramChannelClient | None = None, *, content_resolver=None) -> None:
        self._client = client or TelegramChannelClient()
        self._media = ChannelMediaTransfer(content_resolver)

    def process_message(self, context: Any, channel: ChannelConfig, message: Message) -> None:
        user_id = channel_value(message, "user")
        chat_id = channel_value(message, "chat_id") or channel_value(message, "reply_user")
        message_thread_id = channel_value(message, "message_thread_id")
        thread_id = channel_value(message, "thread_id") or chat_id or user_id
        reply_user = (
            channel_value(message, "reply_user")
            or str((channel.config or {}).get("chat_id", "") or "").strip()
            or chat_id
            or user_id
        )
        context_token = channel_value(message, "context_token") or channel_value(message, "message_id")
        binding_updates = {
            "chat_id": chat_id,
            "message_thread_id": message_thread_id,
            "chat_type": channel_value(message, "chat_type"),
        }

        def _send_reply(content: str, _source_message: Message | None = None) -> None:
            self.send_reply(
                channel,
                receive_id=reply_user,
                content=content,
                context_token=context_token,
                message_thread_id=message_thread_id,
            )

        self._media.process(
            context,
            channel,
            message,
            download=lambda item, folder: self._client.download_media(channel, item, folder),
            send_file=lambda file: self._client.send_file(channel, chat_id=reply_user, file=file,
                context_token=context_token, message_thread_id=message_thread_id),
            binding_key=thread_id or reply_user or user_id,
            user_id=user_id,
            thread_id=thread_id,
            reply_user=reply_user,
            context_token=context_token,
            platform_label="Telegram",
            reply_normalizer=self._client.normalize_reply_text,
            binding_updates=binding_updates,
            reply_sender=_send_reply,
        )

    def send_reply(
        self,
        channel: ChannelConfig,
        *,
        receive_id: str,
        content: str,
        context_token: str = "",
        message_thread_id: str = "",
    ) -> None:
        self._client.send_text_message(
            channel,
            chat_id=receive_id,
            text=content,
            context_token=context_token,
            message_thread_id=message_thread_id,
        )

__all__ = ["TelegramDelivery"]
