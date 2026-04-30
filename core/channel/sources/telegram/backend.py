from __future__ import annotations

from core.channel.models import ChannelConnectionSnapshot
from core.channel.sources.base import ChannelRuntimeBackend
from core.channel.sources.context import ChannelRuntimeContext
from core.channel.sources.telegram.client import TELEGRAM_API_BASE
from core.channel.sources.telegram.runtime_service import TelegramRuntimeSource
from core.config.schema import ChannelConfig
from models.conversation import Conversation, Message


def resolve_telegram_connection_mode(channel: ChannelConfig) -> str:
    config = dict(getattr(channel, "config", {}) or {})
    mode = str(config.get("connection_mode", "polling") or "polling").strip().lower() or "polling"
    if mode not in {"polling"}:
        mode = "polling"
    return mode


class TelegramChannelBackend(ChannelRuntimeBackend):
    channel_type = "telegram"

    def __init__(self, runtime_source: TelegramRuntimeSource | None = None) -> None:
        self._runtime_source = runtime_source or TelegramRuntimeSource()

    def start(self, context: ChannelRuntimeContext, channel: ChannelConfig) -> None:
        context.start_telegram_poller(channel)

    def connection_snapshot(self, context: ChannelRuntimeContext, channel: ChannelConfig) -> ChannelConnectionSnapshot:
        config = dict(getattr(channel, "config", {}) or {})
        bot_token = str(config.get("bot_token", "") or config.get("token", "") or "").strip()
        status_detail = str(config.get("status_detail", "") or "").strip()
        if bot_token:
            detail = "当前使用 Telegram Bot API 长轮询模式：只需 Bot Token，无需公网回调。"
            if status_detail:
                detail += f" {status_detail}"
        else:
            detail = "Telegram 长轮询需要 Bot Token；填写后启用频道即可接收入站消息。"
        return ChannelConnectionSnapshot(
            channel_id=str(getattr(channel, "id", "") or ""),
            channel_type="telegram",
            mode=resolve_telegram_connection_mode(channel),
            status=str(getattr(channel, "status", "draft") or "draft"),
            detail=detail,
            raw={"api_base_url": TELEGRAM_API_BASE, **config},
        )

    def process_message(self, context: ChannelRuntimeContext, channel: ChannelConfig, message: Message) -> None:
        self._runtime_source.process_message(context, channel, message)

    def send_bound_message(
        self,
        context: ChannelRuntimeContext,
        channel: ChannelConfig,
        conversation: Conversation,
        *,
        text: str,
        reply_user: str,
        context_token: str,
    ) -> bool:
        settings = getattr(conversation, "settings", {}) or {}
        binding = settings.get("channel_binding") if isinstance(settings, dict) else None
        message_thread_id = str((binding or {}).get("message_thread_id", "") if isinstance(binding, dict) else "").strip()
        receive_id = str(reply_user or "").strip() or str((binding or {}).get("chat_id", "") if isinstance(binding, dict) else "").strip()
        receive_id = receive_id or str((channel.config or {}).get("chat_id", "") or "").strip()
        if not receive_id:
            raise RuntimeError("当前 Telegram 频道会话还没有可用 Chat ID，无法回发消息。")
        self._runtime_source.send_reply(
            channel,
            receive_id=receive_id,
            content=text,
            context_token=context_token,
            message_thread_id=message_thread_id,
        )
        return True


__all__ = ["TelegramChannelBackend", "resolve_telegram_connection_mode"]