from __future__ import annotations

from typing import Any

from core.channel.models import ChannelConnectionSnapshot
from core.config.schema import ChannelConfig
from models.conversation import Conversation, Message


class ChannelRuntimeBackend:
    """Source runtime backend contract.

    ``ChannelRuntimeService`` owns the shared queue, binding, turn orchestration and
    event projection. Source packages implement connection startup, connection
    snapshots, inbound processing and bound-message replies behind this contract.
    """

    channel_type = ""

    def supports(self, channel: ChannelConfig) -> bool:
        return str(getattr(channel, "type", "") or "").strip().lower() == str(self.channel_type or "").strip().lower()

    def start(self, context: Any, channel: ChannelConfig) -> None:
        return None

    def connection_snapshot(self, context: Any, channel: ChannelConfig) -> ChannelConnectionSnapshot:
        config = dict(getattr(channel, "config", {}) or {})
        mode = str(config.get("connection_mode", "") or "").strip().lower()
        status = str(getattr(channel, "status", "draft") or "draft")
        return ChannelConnectionSnapshot(
            channel_id=str(getattr(channel, "id", "") or ""),
            channel_type=str(getattr(channel, "type", "") or "channel").strip().lower() or "channel",
            mode=mode,
            status=status,
            detail="该频道尚未实现专用连接快照。",
            raw=config,
        )

    def refresh_connection(self, context: Any, channel: ChannelConfig, *, force_new: bool = False) -> ChannelConnectionSnapshot:
        return self.connection_snapshot(context, channel)

    def process_message(self, context: Any, channel: ChannelConfig, message: Message) -> None:
        raise NotImplementedError()

    def send_bound_message(
        self,
        context: Any,
        channel: ChannelConfig,
        conversation: Conversation,
        *,
        text: str,
        reply_user: str,
        context_token: str,
    ) -> bool:
        return False


__all__ = ["ChannelRuntimeBackend"]