from __future__ import annotations

import logging
from typing import Any

from core.channel.sources.message import channel_value
from core.channel.sources.qqbot.client import QQBotChannelClient
from core.config.schema import ChannelConfig
from models.conversation import Message


logger = logging.getLogger(__name__)


class QQBotRuntimeSource:
    """QQ Bot-specific runtime helpers kept outside ``ChannelRuntimeService``."""

    def __init__(self, client: QQBotChannelClient | None = None) -> None:
        self._client = client or QQBotChannelClient()
        self._ws_workers: dict[str, Any] = {}

    def get_ws_worker(self, channel_id: str) -> Any:
        return self._ws_workers.get(str(channel_id or "").strip())

    def remember_ws_worker(self, channel_id: str, handle: Any) -> None:
        normalized_id = str(channel_id or "").strip()
        if normalized_id:
            self._ws_workers[normalized_id] = handle

    def stop(self) -> None:
        from core.channel.sources.qqbot.ws_client import stop_qqbot_ws_client

        for handle in list(self._ws_workers.values()):
            try:
                stop_qqbot_ws_client(handle)
            except Exception as exc:
                logger.debug("Failed to stop QQ Bot websocket worker %s: %s", getattr(handle, "channel_id", ""), exc)

        for handle in list(self._ws_workers.values()):
            try:
                thread = getattr(handle, "thread", None)
                if thread is not None and thread.is_alive():
                    thread.join(timeout=3.0)
            except Exception as exc:
                logger.debug("Failed to join QQ Bot websocket thread %s: %s", getattr(handle, "channel_id", ""), exc)
        self._ws_workers.clear()

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

        processed = context.process_bound_channel_message(
            channel,
            message,
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
        if processed is None:
            return

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

    def normalize_reply_text(self, content: Any, *, limit: int = 2000) -> str:
        return self._client.normalize_reply_text(content, limit=limit)


__all__ = ["QQBotRuntimeSource"]