from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Any, Dict, Iterable, Protocol

from core.config.schema import ChannelConfig


WECHAT_CHERRY_BRIDGE_BASE = "https://ilinkai.weixin.qq.com"


@dataclass(frozen=True)
class ChannelFieldDefinition:
    key: str
    label: str
    placeholder: str = ""
    help_text: str = ""
    required: bool = False
    secret: bool = False
    show_for_modes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChannelDefinition:
    type: str
    name: str
    description: str
    icon_name: str
    default_name: str
    default_config: Dict[str, Any] = field(default_factory=dict)
    fields: tuple[ChannelFieldDefinition, ...] = ()
    summary_keys: tuple[str, ...] = ()
    featured: bool = True
    tags: tuple[str, ...] = ()

    def normalize_source(self, name: str = "") -> str:
        raw_name = str(name or self.default_name or self.name or self.type).strip()
        safe = raw_name.lower().replace(" ", "-") or self.type
        return f"plugin:{self.type}:{safe}"

    def apply_defaults(self, channel: ChannelConfig) -> ChannelConfig:
        merged_config = dict(self.default_config or {})
        merged_config.update(getattr(channel, "config", {}) or {})
        normalized_name = str(getattr(channel, "name", "") or "").strip() or self.default_name or self.name
        normalized_source = str(getattr(channel, "source", "") or "").strip() or self.normalize_source(normalized_name)
        return replace(
            channel,
            type=self.type,
            name=normalized_name,
            source=normalized_source,
            config=merged_config,
        )


@dataclass(frozen=True)
class ChannelInstance:
    definition: ChannelDefinition
    config: ChannelConfig
    summary: str = ""
    validation_errors: tuple[str, ...] = ()

    @property
    def title(self) -> str:
        return str(self.config.name or self.definition.default_name or self.definition.name or self.definition.type).strip()

    @property
    def status_label(self) -> str:
        status = str(getattr(self.config, "status", "") or "draft").strip().lower()
        labels = {
            "draft": "草稿",
            "ready": "就绪",
            "paused": "暂停",
            "error": "异常",
        }
        return labels.get(status, status or "草稿")


class ChannelAdapter(Protocol):
    definition: ChannelDefinition

    def validate(self, channel: ChannelConfig) -> tuple[str, ...]:
        ...

    def summarize(self, channel: ChannelConfig) -> str:
        ...


class DeclarativeChannelAdapter:
    definition: ChannelDefinition

    def __init__(self, definition: ChannelDefinition) -> None:
        self.definition = definition

    def normalize(self, channel: ChannelConfig) -> ChannelConfig:
        return self.definition.apply_defaults(channel)

    def validate(self, channel: ChannelConfig) -> tuple[str, ...]:
        normalized = self.normalize(channel)
        config = dict(getattr(normalized, "config", {}) or {})
        current_mode = str(config.get("connection_mode", "") or "").strip().lower()
        issues: list[str] = []
        for field_def in self.definition.fields:
            if field_def.show_for_modes and current_mode and current_mode not in set(field_def.show_for_modes):
                continue
            if not field_def.required:
                continue
            if str(config.get(field_def.key, "") or "").strip():
                continue
            issues.append(f"缺少必填项：{field_def.label}")
        return tuple(issues)

    def summarize(self, channel: ChannelConfig) -> str:
        normalized = self.normalize(channel)
        config = dict(getattr(normalized, "config", {}) or {})
        parts: list[str] = []
        field_map = {field_def.key: field_def for field_def in self.definition.fields}
        for key in self.definition.summary_keys:
            value = str(config.get(key, "") or "").strip()
            if not value:
                continue
            label = field_map.get(key).label if key in field_map else key
            parts.append(f"{label}: {value}")
        if not parts and str(normalized.source or "").strip():
            parts.append(normalized.source)
        return " · ".join(parts)


class WeChatChannelAdapter(DeclarativeChannelAdapter):
    def __init__(self) -> None:
        super().__init__(
            ChannelDefinition(
                type="wechat",
                name="微信",
                description="同时支持公众号 webhook 与二维码桥接两种接入方式。二维码模式默认对齐 Cherry Studio 的 iLink 扫码基址，也兼容自建 /sessions bridge 服务。",
                icon_name="comments",
                default_name="微信频道",
                default_config={
                    "connection_mode": "official-webhook",
                    "receiver_id": "",
                    "listen_host": "127.0.0.1",
                    "listen_port": "18963",
                    "callback_path": "/wechat",
                    "bridge_api_base": WECHAT_CHERRY_BRIDGE_BASE,
                    "bridge_token": "",
                    "bridge_session_id": "",
                    "qr_login_url": "",
                    "qr_status": "disconnected",
                    "qr_expires_at": "",
                    "connected_account": "",
                    "status_detail": "",
                    "channel_reply_policy": "assistant_messages",
                    "send_thinking_to_channel": False,
                },
                fields=(
                    ChannelFieldDefinition("app_id", "App ID", "wx-app-id", required=True, show_for_modes=("official-webhook",)),
                    ChannelFieldDefinition("app_secret", "App Secret", "请输入 App Secret", required=True, secret=True, show_for_modes=("official-webhook",)),
                    ChannelFieldDefinition("token", "回调 Token", "用于验证回调", required=True, secret=True, show_for_modes=("official-webhook",)),
                    ChannelFieldDefinition("encoding_aes_key", "EncodingAESKey", "可选：当前仅预留，暂不启用 AES 解密", secret=True, show_for_modes=("official-webhook",)),
                    ChannelFieldDefinition("listen_host", "监听地址", "127.0.0.1", show_for_modes=("official-webhook",)),
                    ChannelFieldDefinition("listen_port", "监听端口", "18963", show_for_modes=("official-webhook",)),
                    ChannelFieldDefinition("callback_path", "回调路径", "/wechat/instance-id", show_for_modes=("official-webhook",)),
                    ChannelFieldDefinition("receiver_id", "目标用户 ID", "可选：覆盖默认回发用户（一般留空）", show_for_modes=("official-webhook",)),
                    ChannelFieldDefinition(
                        "bridge_api_base",
                        "扫码桥接地址",
                        WECHAT_CHERRY_BRIDGE_BASE,
                        help_text="默认使用 Cherry Studio 同款 iLink 基址；如接自建 bridge，可改成自己的 /api/wechat 地址。",
                        show_for_modes=("qr-bridge",),
                    ),
                    ChannelFieldDefinition("bridge_token", "桥接 Token", "可选：Bearer Token", secret=True, show_for_modes=("qr-bridge",)),
                ),
                summary_keys=("app_id", "listen_port", "callback_path"),
                tags=("微信", "webhook", "二维码", "国内场景"),
            )
        )

    @staticmethod
    def _connection_mode(channel: ChannelConfig) -> str:
        config = dict(getattr(channel, "config", {}) or {})
        mode = str(config.get("connection_mode", "official-webhook") or "official-webhook").strip().lower()
        return mode or "official-webhook"

    def validate(self, channel: ChannelConfig) -> tuple[str, ...]:
        normalized = self.normalize(channel)
        config = dict(getattr(normalized, "config", {}) or {})
        mode = self._connection_mode(normalized)
        if mode == "qr-bridge":
            bridge_api_base = str(config.get("bridge_api_base", "") or "").strip()
            qr_login_url = str(config.get("qr_login_url", "") or "").strip()
            if bridge_api_base or qr_login_url:
                return ()
            return ("缺少必填项：扫码桥接地址",)
        return super().validate(normalized)

    def summarize(self, channel: ChannelConfig) -> str:
        normalized = self.normalize(channel)
        config = dict(getattr(normalized, "config", {}) or {})
        mode = self._connection_mode(normalized)
        if mode == "qr-bridge":
            status_raw = str(config.get("qr_status", "disconnected") or "disconnected").strip().lower() or "disconnected"
            status_label = {
                "draft": "草稿",
                "disconnected": "未连接",
                "wait": "待扫码",
                "pending": "待扫码",
                "waiting": "待扫码",
                "scaned": "已扫码待确认",
                "scanned": "已扫码待确认",
                "confirmed": "已连接",
                "connected": "已连接",
                "ready": "已连接",
                "expired": "已失效",
                "error": "异常",
            }.get(status_raw, status_raw)
            parts = ["二维码连接", f"状态: {status_label}"]
            session_id = str(config.get("bridge_session_id", "") or "").strip()
            account = str(config.get("connected_account", "") or "").strip()
            if account:
                parts.append(f"账号: {account}")
            elif session_id:
                parts.append(f"会话: {session_id}")
            return " · ".join(parts)
        return super().summarize(normalized)


class QQBotChannelAdapter(DeclarativeChannelAdapter):
    def __init__(self) -> None:
        super().__init__(
            ChannelDefinition(
                type="qqbot",
                name="QQ Bot",
                description="通过 QQ 官方 Gateway 长连接接入机器人，只需 App ID 与 App Secret，适合 QQ 群聊、单聊和频道问答。",
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


class FeishuChannelAdapter(DeclarativeChannelAdapter):
    def __init__(self) -> None:
        super().__init__(
            ChannelDefinition(
                type="feishu",
                name="飞书",
                description="默认使用飞书长连接模式直连开放平台，也兼容高级场景下的 webhook 回调模式，适合企业通知、群机器人和内部问答入口。",
                icon_name="book-open",
                default_name="飞书频道",
                default_config={
                    "connection_mode": "websocket",
                    "listen_host": "127.0.0.1",
                    "listen_port": "18964",
                    "callback_path": "/feishu",
                    "chat_id": "",
                    "open_base_url": "https://open.feishu.cn",
                    "status_detail": "",
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
                    ChannelFieldDefinition("chat_id", "目标群组 / 会话 ID", "如需要固定回发目标，可填写"),
                    ChannelFieldDefinition("open_base_url", "开放平台地址", "https://open.feishu.cn", help_text="默认使用飞书开放平台中国站地址；国际版可改为 Lark 域名。"),
                ),
                summary_keys=("app_id", "chat_id", "listen_port", "callback_path"),
                tags=("飞书", "企业办公", "机器人"),
            )
        )


class TelegramChannelAdapter(DeclarativeChannelAdapter):
    def __init__(self) -> None:
        super().__init__(
            ChannelDefinition(
                type="telegram",
                name="Telegram",
                description="通过 Telegram Bot API 长轮询接入机器人，只需 Bot Token，无需公网回调；Chat ID 可作为手动测试或固定回发目标。",
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


class WebhookChannelAdapter(DeclarativeChannelAdapter):
    def __init__(self) -> None:
        super().__init__(
            ChannelDefinition(
                type="webhook",
                name="Webhook",
                description="保留轻量通用入口，用于兼容已有 webhook / sidecar 集成。",
                icon_name="link",
                default_name="Webhook",
                featured=False,
                fields=(
                    ChannelFieldDefinition("webhook_url", "Webhook 地址", "https://...", required=True),
                    ChannelFieldDefinition("signing_secret", "签名密钥", "可选：用于验签", secret=True),
                ),
                summary_keys=("webhook_url",),
                tags=("兼容", "通用"),
            )
        )


class CustomChannelAdapter(DeclarativeChannelAdapter):
    def __init__(self) -> None:
        super().__init__(
            ChannelDefinition(
                type="custom",
                name="自定义",
                description="用于保留实验型或桥接型来源，避免把业务方的特殊接入硬编码进主界面。",
                icon_name="sliders",
                default_name="自定义来源",
                featured=False,
                fields=(
                    ChannelFieldDefinition("endpoint", "入口地址 / 标识", "例如本地桥接服务地址", required=True),
                    ChannelFieldDefinition("token", "访问 Token", "可选：用于鉴权", secret=True),
                ),
                summary_keys=("endpoint",),
                tags=("实验", "桥接"),
            )
        )


class ChannelManager:
    def __init__(self, adapters: Iterable[ChannelAdapter] | None = None) -> None:
        self._adapters: dict[str, ChannelAdapter] = {}
        for adapter in adapters or ():
            self.register(adapter)

    def register(self, adapter: ChannelAdapter) -> None:
        channel_type = str(adapter.definition.type or "").strip().lower()
        if not channel_type:
            raise ValueError("channel adapter requires a non-empty type")
        self._adapters[channel_type] = adapter

    def adapters(self) -> tuple[ChannelAdapter, ...]:
        return tuple(self._adapters.values())

    def definitions(self, *, featured_only: bool = False) -> tuple[ChannelDefinition, ...]:
        definitions = [adapter.definition for adapter in self._adapters.values()]
        if featured_only:
            definitions = [definition for definition in definitions if definition.featured]
        return tuple(definitions)

    def get_adapter(self, channel_type: str) -> ChannelAdapter:
        key = str(channel_type or "").strip().lower()
        return self._adapters.get(key) or self._adapters["custom"]

    def get_definition(self, channel_type: str) -> ChannelDefinition:
        return self.get_adapter(channel_type).definition

    def ensure_channel(self, channel: ChannelConfig) -> ChannelConfig:
        adapter = self.get_adapter(getattr(channel, "type", "") or "custom")
        if isinstance(adapter, DeclarativeChannelAdapter):
            return adapter.normalize(channel)
        return channel

    def build_instance(self, channel: ChannelConfig) -> ChannelInstance:
        normalized = self.ensure_channel(channel)
        adapter = self.get_adapter(normalized.type)
        return ChannelInstance(
            definition=adapter.definition,
            config=normalized,
            summary=adapter.summarize(normalized),
            validation_errors=adapter.validate(normalized),
        )

    def validate(self, channel: ChannelConfig) -> tuple[str, ...]:
        return self.build_instance(channel).validation_errors

    def summarize(self, channel: ChannelConfig) -> str:
        return self.build_instance(channel).summary

    def featured_types(self) -> tuple[str, ...]:
        return tuple(definition.type for definition in self.definitions(featured_only=True))


@lru_cache(maxsize=1)
def default_channel_manager() -> ChannelManager:
    return ChannelManager(
        adapters=(
            WeChatChannelAdapter(),
            QQBotChannelAdapter(),
            FeishuChannelAdapter(),
            TelegramChannelAdapter(),
            WebhookChannelAdapter(),
            CustomChannelAdapter(),
        )
    )


def get_default_channel_definitions(*, featured_only: bool = False) -> tuple[ChannelDefinition, ...]:
    return default_channel_manager().definitions(featured_only=featured_only)
