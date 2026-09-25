from __future__ import annotations

from pycat.core.channel.catalog import ChannelDefinition, ChannelFieldDefinition, DeclarativeChannelDefinition


class FeishuChannelDefinition(DeclarativeChannelDefinition):
    def __init__(self) -> None:
        super().__init__(
            ChannelDefinition(
                type="feishu",
                name="飞书",
                description="默认使用飞书长连接模式直连开放平台，也兼容高级场景下的 webhook 回调模式。",
                icon_name="comments",
                default_name="飞书频道",
                default_config={
                    "connection_mode": "websocket",
                    "listen_host": "127.0.0.1",
                    "listen_port": "18964",
                    "callback_path": "/feishu",
                    "chat_id": "",
                    "open_base_url": "https://open.feishu.cn",
                    "channel_reply_policy": "assistant_messages",
                    "send_thinking_to_channel": False,
                },
                fields=(
                    ChannelFieldDefinition("app_id", "App ID", "cli_xxx", required=True),
                    ChannelFieldDefinition("app_secret", "App Secret", "请输入 App Secret", required=True, secret=True),
                    ChannelFieldDefinition("verification_token", "Verification Token", "回调校验 Token", required=True, secret=True, show_for_modes=("webhook",)),
                    ChannelFieldDefinition("encrypt_key", "Encrypt Key", "可选：暂未实现加密回调解密", secret=True, show_for_modes=("webhook",)),
                    ChannelFieldDefinition("listen_host", "监听地址", "127.0.0.1", show_for_modes=("webhook",)),
                    ChannelFieldDefinition("listen_port", "监听端口", "18964", show_for_modes=("webhook",)),
                    ChannelFieldDefinition("callback_path", "回调路径", "/feishu/instance-id", show_for_modes=("webhook",)),
                    ChannelFieldDefinition("chat_id", "目标群组 / 会话 ID", "如需固定回发目标，可填写"),
                    ChannelFieldDefinition("open_base_url", "开放平台地址", "https://open.feishu.cn", help_text="默认使用飞书开放平台中国站地址；国际版可改为 Lark 域名。"),
                ),
                summary_keys=("app_id", "chat_id", "listen_port", "callback_path"),
                tags=("飞书", "企业办公", "机器人"),
            )
        )


__all__ = ["FeishuChannelDefinition"]
