from __future__ import annotations

from pycat.core.channel.platforms.telegram.backend import (
    TelegramChannelPlatformBackend,
    resolve_telegram_connection_mode,
)
from pycat.core.channel.platforms.telegram.client import TELEGRAM_API_BASE, TelegramChannelClient
from pycat.core.channel.platforms.telegram.poller import (
    TelegramPollerHandle,
    start_telegram_poller,
    stop_telegram_poller,
)
from pycat.core.channel.platforms.telegram.router import (
    TelegramUpdateEnvelope,
    extract_telegram_message_text,
    normalize_telegram_update,
)

__all__ = [
    "TELEGRAM_API_BASE",
    "TelegramChannelPlatformBackend",
    "TelegramChannelClient",
    "TelegramPollerHandle",
    "TelegramUpdateEnvelope",
    "extract_telegram_message_text",
    "normalize_telegram_update",
    "resolve_telegram_connection_mode",
    "start_telegram_poller",
    "stop_telegram_poller",
]
