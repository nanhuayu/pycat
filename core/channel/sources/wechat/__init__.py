from __future__ import annotations

from core.channel.sources.wechat.client import WECHAT_CHERRY_BRIDGE_BASE, WeChatChannelClient
from core.channel.sources.wechat.bridge_poller import (
    run_wechat_bridge_worker_loop,
    start_wechat_bridge_worker,
)
from core.channel.sources.wechat.protocol import (
    WeChatInboundMessage,
    build_wechat_text_reply,
    normalize_wechat_reply_text,
    parse_wechat_message,
    verify_wechat_signature,
)
from core.channel.sources.wechat.router import (
    WeChatBridgeInboundEnvelope,
    extract_wechat_bridge_text,
    normalize_wechat_bridge_message,
)
from core.channel.sources.wechat.backend import WeChatChannelBackend
from core.channel.sources.wechat.webhook_server import start_wechat_webhook_server


__all__ = [
    "WECHAT_CHERRY_BRIDGE_BASE",
    "WeChatBridgeInboundEnvelope",
    "WeChatChannelBackend",
    "WeChatChannelClient",
    "WeChatInboundMessage",
    "build_wechat_text_reply",
    "extract_wechat_bridge_text",
    "normalize_wechat_bridge_message",
    "normalize_wechat_reply_text",
    "parse_wechat_message",
    "run_wechat_bridge_worker_loop",
    "start_wechat_bridge_worker",
    "start_wechat_webhook_server",
    "verify_wechat_signature",
]