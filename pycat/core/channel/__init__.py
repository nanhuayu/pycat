from __future__ import annotations

from pycat.core.channel.bindings import ChannelConversationBindingStore
from pycat.core.channel.catalog import (
    ChannelCatalog,
    ChannelDefinition,
    ChannelFieldDefinition,
    ChannelInstance,
    build_channel_catalog,
)
from pycat.core.channel.connection import (
    ChannelConnectionHandle,
    ChannelConnectionSnapshot,
    ChannelConnectionState,
    ChannelRequiredAction,
)
from pycat.core.channel.envelope import (
    ChannelEnvelope,
    ChannelInbound,
    ChannelOrigin,
    channel_metadata,
    channel_origin_from_message,
    channel_value,
    message_from_channel,
    parse_channel_message,
    wrap_channel_message,
)
from pycat.core.channel.events import ChannelEvent
from pycat.core.channel.host import ChannelHost
from pycat.core.channel.platforms import ChannelPlatformBackend
from pycat.core.channel.prompt import build_channel_prompt_section
from pycat.core.channel.queue import ChannelQueue
from pycat.core.channel.sessions import ChannelConversationSummary
from pycat.models.contracts.channel import ChannelConfig

if False:  # pragma: no cover
    from pycat.core.channel.gateway import ChannelGateway


def __getattr__(name: str):
    if name == "ChannelGateway":
        from pycat.core.channel.gateway import ChannelGateway

        return ChannelGateway
    raise AttributeError(name)

__all__ = [
    "ChannelEnvelope",
    "ChannelCatalog",
    "ChannelConfig",
    "ChannelConnectionHandle",
    "ChannelConnectionSnapshot",
    "ChannelConnectionState",
    "ChannelConversationBindingStore",
    "ChannelConversationSummary",
    "ChannelPlatformBackend",
    "ChannelDefinition",
    "ChannelFieldDefinition",
    "ChannelHost",
    "ChannelInbound",
    "ChannelInstance",
    "ChannelOrigin",
    "ChannelQueue",
    "ChannelRequiredAction",
    "ChannelEvent",
    "ChannelGateway",
    "build_channel_catalog",
    "build_channel_prompt_section",
    "channel_metadata",
    "channel_origin_from_message",
    "channel_value",
    "message_from_channel",
    "parse_channel_message",
    "wrap_channel_message",
]
