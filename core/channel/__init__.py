from __future__ import annotations

from core.channel.protocol import (
    ChannelEnvelope,
    ChannelInbound,
    ChannelOrigin,
    ChannelQueue,
    build_channel_prompt_section,
    channel_metadata,
    channel_origin_from_message,
    message_from_channel,
    parse_channel_message,
    wrap_channel_message,
)
from core.channel.sources import ChannelRuntimeBackend, FeishuChannelBackend, QQBotChannelBackend, WeChatChannelBackend
from core.channel.models import (
    ChannelConnectionSnapshot,
    ChannelConversationBindingStore,
    ChannelConversationSummary,
    ChannelRuntimeEvent,
)
from core.channel.registry import (
    ChannelDefinition,
    ChannelFieldDefinition,
    ChannelInstance,
    ChannelManager,
    default_channel_manager,
    get_default_channel_definitions,
)

if False:  # pragma: no cover
    from core.channel.runtime import ChannelRuntimeService


def __getattr__(name: str):
    if name == "ChannelRuntimeService":
        from core.channel.runtime import ChannelRuntimeService

        return ChannelRuntimeService
    raise AttributeError(name)

__all__ = [
    "ChannelEnvelope",
    "ChannelConnectionSnapshot",
    "ChannelConversationBindingStore",
    "ChannelConversationSummary",
    "ChannelRuntimeBackend",
    "ChannelDefinition",
    "ChannelFieldDefinition",
    "FeishuChannelBackend",
    "QQBotChannelBackend",
    "ChannelInbound",
    "ChannelInstance",
    "ChannelManager",
    "ChannelOrigin",
    "ChannelQueue",
    "ChannelRuntimeEvent",
    "ChannelRuntimeService",
    "WeChatChannelBackend",
    "build_channel_prompt_section",
    "channel_metadata",
    "channel_origin_from_message",
    "default_channel_manager",
    "get_default_channel_definitions",
    "message_from_channel",
    "parse_channel_message",
    "wrap_channel_message",
]
