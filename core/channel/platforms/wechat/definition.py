from __future__ import annotations

from core.channel.catalog import ChannelDefinition, ChannelFieldDefinition, DeclarativeChannelDefinition
from models.contracts.channel import ChannelConfig


class WeChatChannelDefinition(DeclarativeChannelDefinition):
    def __init__(self) -> None:
        super().__init__(
            ChannelDefinition(
                type="wechat",
                name="微信",
                description=(
                    "支持个人微信扫码（实验）与公众号 Webhook（稳定）。"
                ),
                icon_name="comments",
                default_name="微信频道",
                default_config={
                    "connection_mode": "ilink",
                    "receiver_id": "",
                    "listen_host": "127.0.0.1",
                    "listen_port": "18963",
                    "callback_path": "/wechat",
                    "channel_reply_policy": "assistant_messages",
                    "send_thinking_to_channel": False,
                },
                fields=(
                    ChannelFieldDefinition("app_id", "App ID", "wx-app-id", required=True, show_for_modes=("webhook",)),
                    ChannelFieldDefinition("app_secret", "App Secret", "请输入 App Secret", required=True, secret=True, show_for_modes=("webhook",)),
                    ChannelFieldDefinition("token", "回调 Token", "用于验证回调", required=True, secret=True, show_for_modes=("webhook",)),
                    ChannelFieldDefinition("encoding_aes_key", "EncodingAESKey", "可选：当前仅预留，暂不启用 AES 解密", secret=True, show_for_modes=("webhook",)),
                    ChannelFieldDefinition("listen_host", "监听地址", "127.0.0.1", show_for_modes=("webhook",)),
                    ChannelFieldDefinition("listen_port", "监听端口", "18963", show_for_modes=("webhook",)),
                    ChannelFieldDefinition("callback_path", "回调路径", "/wechat/instance-id", show_for_modes=("webhook",)),
                    ChannelFieldDefinition("receiver_id", "目标用户 ID", "可选：覆盖默认回发用户（一般留空）", show_for_modes=("webhook",)),
                ),
                summary_keys=("app_id", "listen_port", "callback_path"),
                tags=("微信", "webhook", "二维码", "国内场景"),
            )
        )

    @staticmethod
    def _connection_mode(channel: ChannelConfig) -> str:
        config = dict(getattr(channel, "config", {}) or {})
        mode = str(config.get("connection_mode", "ilink") or "ilink").strip().lower()
        return mode if mode in {"ilink", "webhook"} else "ilink"

    def validate(self, channel: ChannelConfig) -> tuple[str, ...]:
        normalized = self.normalize(channel)
        return super().validate(normalized)

    def normalize(self, channel: ChannelConfig) -> ChannelConfig:
        normalized = super().normalize(channel)
        config = dict(getattr(normalized, "config", {}) or {})
        config["connection_mode"] = self._connection_mode(normalized)
        return ChannelConfig.from_dict({**normalized.to_dict(), "config": config})

    def summarize(self, channel: ChannelConfig) -> str:
        normalized = self.normalize(channel)
        config = dict(getattr(normalized, "config", {}) or {})
        mode = self._connection_mode(normalized)
        if mode == "ilink":
            parts = ["个人微信扫码（实验）"]
            account = str(config.get("ilink_user_id", "") or "").strip()
            if account:
                parts.append(f"账号: {account}")
            return " · ".join(parts)
        return super().summarize(normalized)


__all__ = ["WeChatChannelDefinition"]
