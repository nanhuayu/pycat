from __future__ import annotations

from core.channel.sources.telegram.backend import TelegramChannelBackend, resolve_telegram_connection_mode
from core.channel.sources.telegram.client import TELEGRAM_API_BASE, TelegramChannelClient
from core.channel.sources.telegram.poller import TelegramPollerHandle, start_telegram_poller, stop_telegram_poller
from core.channel.sources.telegram.router import TelegramUpdateEnvelope, extract_telegram_message_text, normalize_telegram_update


__all__ = [
    "TELEGRAM_API_BASE",
    "TelegramChannelBackend",
    "TelegramChannelClient",
    "TelegramPollerHandle",
    "TelegramUpdateEnvelope",
    "extract_telegram_message_text",
    "normalize_telegram_update",
    "resolve_telegram_connection_mode",
    "start_telegram_poller",
    "stop_telegram_poller",
]