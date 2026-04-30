from __future__ import annotations

from core.channel.sources.session import (
    ChannelSessionRuntimeAdapter,
    ResolvedChannelConversation,
)


ResolvedWeChatConversation = ResolvedChannelConversation


class WeChatRuntimeAdapter(ChannelSessionRuntimeAdapter):
    """Backward-compatible wrapper around the generic session runtime adapter."""

    def resolve_conversation(self, channel, user_id: str):
        return super().resolve_conversation(channel, user_id, user_id=user_id)


__all__ = ["ResolvedWeChatConversation", "WeChatRuntimeAdapter"]