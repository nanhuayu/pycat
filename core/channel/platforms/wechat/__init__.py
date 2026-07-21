from __future__ import annotations

from core.channel.platforms.wechat.backend import WeChatChannelPlatformBackend
from core.channel.platforms.wechat.client import WeChatChannelClient
from core.channel.platforms.wechat.login import WeChatLoginFlow, WeChatLoginSession
from core.channel.platforms.wechat.protocol import (
    WeChatInboundMessage,
    build_wechat_text_reply,
    normalize_wechat_reply_text,
    parse_wechat_message,
    verify_wechat_signature,
)
from core.channel.platforms.wechat.router import (
    WeChatILinkInboundEnvelope,
    extract_wechat_ilink_text,
    normalize_wechat_ilink_message,
)


__all__ = [
    "WeChatChannelPlatformBackend",
    "WeChatChannelClient",
    "WeChatILinkInboundEnvelope",
    "WeChatInboundMessage",
    "WeChatLoginFlow",
    "WeChatLoginSession",
    "build_wechat_text_reply",
    "extract_wechat_ilink_text",
    "normalize_wechat_ilink_message",
    "normalize_wechat_reply_text",
    "parse_wechat_message",
    "verify_wechat_signature",
]
