from __future__ import annotations

import logging
from typing import Any

from core.channel.sources.feishu.client import FeishuChannelClient
from core.channel.sources.message import channel_value
from core.config.schema import ChannelConfig
from models.conversation import Message


logger = logging.getLogger(__name__)


class FeishuRuntimeSource:
    """Feishu-specific runtime helpers kept outside ``ChannelRuntimeService``."""

    def __init__(self, client: FeishuChannelClient | None = None) -> None:
        self._client = client or FeishuChannelClient()
        self._ws_workers: dict[str, Any] = {}

    def get_ws_worker(self, channel_id: str) -> Any:
        return self._ws_workers.get(str(channel_id or "").strip())

    def remember_ws_worker(self, channel_id: str, handle: Any) -> None:
        normalized_id = str(channel_id or "").strip()
        if normalized_id:
            self._ws_workers[normalized_id] = handle

    def stop(self) -> None:
        from core.channel.sources.feishu.ws_client import stop_feishu_ws_client

        for handle in list(self._ws_workers.values()):
            try:
                stop_feishu_ws_client(handle)
            except Exception as exc:
                logger.debug("Failed to stop Feishu websocket worker %s: %s", getattr(handle, "channel_id", ""), exc)

        for handle in list(self._ws_workers.values()):
            try:
                thread = getattr(handle, "thread", None)
                if thread is not None and thread.is_alive():
                    thread.join(timeout=3.0)
            except Exception as exc:
                logger.debug("Failed to join Feishu websocket thread %s: %s", getattr(handle, "channel_id", ""), exc)
        self._ws_workers.clear()

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

        processed = context.process_bound_channel_message(
            channel,
            message,
            binding_key=thread_id or reply_user or user_id,
            user_id=user_id,
            thread_id=thread_id,
            reply_user=reply_user,
            context_token="",
            platform_label="Feishu",
            reply_normalizer=self._client.normalize_reply_text,
            reply_sender=_send_reply,
        )
        if processed is None:
            return

    def send_reply(self, channel: ChannelConfig, *, receive_id: str, content: str) -> None:
        self._client.send_text_message(channel, receive_id=receive_id, text=content, receive_id_type="chat_id")

    def normalize_reply_text(self, content: Any, *, limit: int = 4000) -> str:
        return self._client.normalize_reply_text(content, limit=limit)


__all__ = ["FeishuRuntimeSource"]