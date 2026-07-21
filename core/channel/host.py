from __future__ import annotations

from typing import Any, Callable

from core.channel.connection import ChannelConnectionHandle, ChannelConnectionSnapshot
from models.contracts.channel import ChannelConfig
from models.conversation import Conversation, Message


class ChannelHost:
    """Generic host capabilities exposed to platform implementations."""

    def __init__(self, gateway: Any) -> None:
        self._gateway = gateway

    def is_stopping(self) -> bool:
        return bool(self._gateway.is_stopping)

    def get_channel(self, channel_id: str) -> ChannelConfig | None:
        return self._gateway.get_channel(channel_id)

    def remember_channel(self, channel: ChannelConfig) -> None:
        self._gateway.remember_channel(channel)

    def mark_recent_message(self, channel_id: str, message_key: str) -> bool:
        return self._gateway.mark_recent_message(channel_id, message_key)

    def enqueue_channel_message(self, channel: ChannelConfig, content: str, *, meta: dict[str, Any] | None = None) -> None:
        self._gateway.enqueue_channel_message(channel, content, meta=meta)

    def process_bound_channel_message(
        self,
        channel: ChannelConfig,
        message: Message,
        *,
        binding_key: str,
        user_id: str,
        thread_id: str,
        reply_user: str,
        context_token: str,
        platform_label: str,
        reply_normalizer: Callable[[str], str] | None = None,
        binding_updates: dict[str, Any] | None = None,
        reply_sender: Callable[[str, Message | None], None] | None = None,
    ) -> tuple[Conversation, str] | None:
        return self._gateway.process_bound_channel_message(
            channel,
            message,
            binding_key=binding_key,
            user_id=user_id,
            thread_id=thread_id,
            reply_user=reply_user,
            context_token=context_token,
            platform_label=platform_label,
            reply_normalizer=reply_normalizer,
            binding_updates=binding_updates,
            reply_sender=reply_sender,
        )

    def report_connection(self, snapshot: ChannelConnectionSnapshot) -> None:
        self._gateway.report_connection(snapshot)

    def connection_snapshot(self, channel_id: str) -> ChannelConnectionSnapshot | None:
        return self._gateway.runtime_connection_snapshot(channel_id)

    def remember_connection_handle(self, handle: ChannelConnectionHandle) -> None:
        self._gateway.remember_connection_handle(handle)


__all__ = ["ChannelHost"]
