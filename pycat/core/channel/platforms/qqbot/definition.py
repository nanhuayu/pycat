from __future__ import annotations

from pycat.core.channel.catalog import ChannelDefinition, ChannelFieldDefinition, DeclarativeChannelDefinition


class QQBotChannelDefinition(DeclarativeChannelDefinition):
    def __init__(self) -> None:
        super().__init__(
            ChannelDefinition(
                type="qqbot",
                name="QQ Bot",
                description="通过 QQ 官方 Gateway 长连接接入机器人，适合 QQ 群聊、单聊和频道问答。",
                icon_name="users",
                default_name="QQ Bot",
                default_config={
                    "connection_mode": "websocket",
                    "listen_host": "127.0.0.1",
                    "listen_port": "18965",
                    "callback_path": "/qqbot",
                    "target_id": "",
                    "target_type": "channel",
                    "api_base_url": "https://api.sgroup.qq.com",
                    "send_endpoint": "",
                    "webhook_token": "",
                    "sandbox": False,
                    "channel_reply_policy": "assistant_messages",
                    "send_thinking_to_channel": False,
                },
                fields=(
                    ChannelFieldDefinition("app_id", "App ID", "QQ Bot App ID", required=True),
                    ChannelFieldDefinition("app_secret", "App Secret", "请输入 App Secret", required=True, secret=True),
                    ChannelFieldDefinition("listen_host", "监听地址", "127.0.0.1", show_for_modes=("webhook",)),
                    ChannelFieldDefinition("listen_port", "监听端口", "18965", show_for_modes=("webhook",)),
                    ChannelFieldDefinition("callback_path", "回调路径", "/qqbot/instance-id", show_for_modes=("webhook",)),
                    ChannelFieldDefinition("sandbox", "沙箱环境", "true / false"),
                ),
                summary_keys=("app_id", "sandbox"),
                tags=("QQ", "社区", "机器人"),
            )
        )


__all__ = ["QQBotChannelDefinition"]
