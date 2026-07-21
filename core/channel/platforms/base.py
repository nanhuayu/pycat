from __future__ import annotations

from typing import Any

from core.channel.connection import ChannelConnectionSnapshot, ChannelConnectionState
from core.channel.host import ChannelHost
from models.contracts.channel import ChannelConfig
from models.conversation import Conversation, Message


class ChannelPlatformBackend:
    """Source runtime backend contract.

    ``ChannelGateway`` owns the shared queue, binding, turn orchestration and
    event projection. Source packages implement connection startup, connection
    snapshots, inbound processing and bound-message replies behind this contract.
    """

    channel_type = ""

    def supports(self, channel: ChannelConfig) -> bool:
        return str(getattr(channel, "type", "") or "").strip().lower() == str(self.channel_type or "").strip().lower()

    def start(self, context: ChannelHost, channel: ChannelConfig) -> None:
        return None

    def connection_snapshot(self, context: ChannelHost, channel: ChannelConfig) -> ChannelConnectionSnapshot:
        config = dict(getattr(channel, "config", {}) or {})
        mode = str(config.get("connection_mode", "") or "").strip().lower()
        return ChannelConnectionSnapshot(
            channel_id=str(getattr(channel, "id", "") or ""),
            channel_type=str(getattr(channel, "type", "") or "channel").strip().lower() or "channel",
            mode=mode,
            state=ChannelConnectionState.INCOMPLETE,
            detail="该频道尚未实现专用连接快照。",
            raw=config,
        )

    def process_message(self, context: ChannelHost, channel: ChannelConfig, message: Message) -> None:
        raise NotImplementedError()

    def send_bound_message(
        self,
        context: ChannelHost,
        channel: ChannelConfig,
        conversation: Conversation,
        *,
        text: str,
        reply_user: str,
        context_token: str,
    ) -> bool:
        return False


__all__ = ["ChannelPlatformBackend"]
