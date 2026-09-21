from __future__ import annotations

from pycat.core.channel.catalog import ChannelDefinition, ChannelFieldDefinition, DeclarativeChannelDefinition


class TelegramChannelDefinition(DeclarativeChannelDefinition):
    def __init__(self) -> None:
        super().__init__(
            ChannelDefinition(
                type="telegram",
                name="Telegram",
                description="通过 Telegram Bot API 长轮询接入机器人，只需 Bot Token；Chat ID 可用于手动测试或固定回发目标。",
                icon_name="paper-plane",
                default_name="Telegram Bot",
                default_config={
                    "connection_mode": "polling",
                    "api_base_url": "https://api.telegram.org",
                    "chat_id": "",
                    "proxy_url": "",
                    "poll_timeout": "25",
                    "allowed_updates": "",
                    "channel_reply_policy": "assistant_messages",
                    "send_thinking_to_channel": False,
                },
                fields=(
                    ChannelFieldDefinition("bot_token", "Bot Token", "123456:ABC-DEF", required=True, secret=True),
                    ChannelFieldDefinition("chat_id", "Chat ID", "可选：固定回发目标或测试会话，例如 -100xxxxxxxxxx"),
                    ChannelFieldDefinition("proxy_url", "代理地址", "可选：socks5:// 或 https://"),
                ),
                summary_keys=("chat_id", "proxy_url"),
                tags=("Telegram", "跨平台", "开发者"),
            )
        )


__all__ = ["TelegramChannelDefinition"]
