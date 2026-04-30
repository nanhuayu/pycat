from __future__ import annotations

import logging
from typing import Any

from core.channel.sources.message import channel_value
from core.channel.sources.telegram.client import TelegramChannelClient
from core.config.schema import ChannelConfig
from models.conversation import Message


logger = logging.getLogger(__name__)


class TelegramRuntimeSource:
    """Telegram-specific runtime helpers kept outside ``ChannelRuntimeService``."""

    def __init__(self, client: TelegramChannelClient | None = None) -> None:
        self._client = client or TelegramChannelClient()
        self._poll_workers: dict[str, Any] = {}

    def get_poller_worker(self, channel_id: str) -> Any:
        return self._poll_workers.get(str(channel_id or "").strip())

    def remember_poller_worker(self, channel_id: str, handle: Any) -> None:
        normalized_id = str(channel_id or "").strip()
        if normalized_id:
            self._poll_workers[normalized_id] = handle

    def stop(self) -> None:
        from core.channel.sources.telegram.poller import stop_telegram_poller

        for handle in list(self._poll_workers.values()):
            try:
                stop_telegram_poller(handle)
            except Exception as exc:
                logger.debug("Failed to stop Telegram poller %s: %s", getattr(handle, "channel_id", ""), exc)

        for handle in list(self._poll_workers.values()):
            try:
                thread = getattr(handle, "thread", None)
                if thread is not None and thread.is_alive():
                    thread.join(timeout=3.0)
            except Exception as exc:
                logger.debug("Failed to join Telegram poller thread %s: %s", getattr(handle, "channel_id", ""), exc)
        self._poll_workers.clear()

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
        processed = context.process_bound_channel_message(
            channel,
            message,
            binding_key=thread_id or reply_user or user_id,
            user_id=user_id,
            thread_id=thread_id,
            reply_user=reply_user,
            context_token=context_token,
            platform_label="Telegram",
            reply_normalizer=self._client.normalize_reply_text,
            binding_updates=binding_updates,
        )
        if processed is None:
            return
        _, reply_text = processed

        try:
            self.send_reply(
                channel,
                receive_id=reply_user,
                content=reply_text,
                context_token=context_token,
                message_thread_id=message_thread_id,
            )
        except Exception as exc:
            logger.warning("Failed to send Telegram reply for channel %s: %s", getattr(channel, "id", ""), exc)

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

    def normalize_reply_text(self, content: Any, *, limit: int = 4096) -> str:
        return self._client.normalize_reply_text(content, limit=limit)


__all__ = ["TelegramRuntimeSource"]