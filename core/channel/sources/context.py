from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.channel.models import ChannelConnectionSnapshot, ChannelServerHandle, _WeChatBridgePollerHandle
from core.config.schema import ChannelConfig
from models.conversation import Conversation, Message

if TYPE_CHECKING:
    from core.channel.sources.feishu.runtime_service import FeishuRuntimeSource
    from core.channel.sources.qqbot.runtime_service import QQBotRuntimeSource
    from core.channel.sources.telegram.runtime_service import TelegramRuntimeSource
    from core.channel.sources.wechat.runtime_service import WeChatRuntimeSource


class ChannelRuntimeContext:
    """Narrow facade exposed to source-specific channel implementations.

    ``ChannelRuntimeService`` keeps the shared queue, binding and turn orchestration.
    WeChat / Feishu packages call this context instead of reaching into runtime
    private methods directly. The methods are intentionally grouped by source so
    the next migration can move the underlying implementations into each source
    folder without changing the backend contract again.
    """

    def __init__(
        self,
        runtime: Any,
        *,
        wechat_source: WeChatRuntimeSource,
        feishu_source: FeishuRuntimeSource,
        qqbot_source: QQBotRuntimeSource,
        telegram_source: TelegramRuntimeSource,
    ) -> None:
        self._runtime = runtime
        self._wechat_source = wechat_source
        self._feishu_source = feishu_source
        self._qqbot_source = qqbot_source
        self._telegram_source = telegram_source

    # Generic runtime helpers ------------------------------------------------------
    def is_stopping(self) -> bool:
        return bool(self._runtime.is_stopping)

    def get_channel(self, channel_id: str) -> ChannelConfig | None:
        return self._runtime.get_runtime_channel(channel_id)

    def remember_channel(self, channel: ChannelConfig) -> None:
        self._runtime.remember_runtime_channel(channel)

    def mark_recent_message(self, channel_id: str, message_key: str) -> bool:
        return self._runtime.mark_recent_message(channel_id, message_key)

    def enqueue_channel_message(self, channel: ChannelConfig, content: str, *, meta: dict[str, Any] | None = None) -> None:
        self._runtime.enqueue_channel_message(channel, content, meta=meta)

    def process_bound_channel_message(
        self,
        channel: ChannelConfig,
        message: Message,
        *,
        binding_key: str,
        user_id: str,
        thread_id: str,
        reply_user: str,
        context_token: str,
        platform_label: str,
        reply_normalizer: Any | None = None,
        binding_updates: dict[str, Any] | None = None,
    ) -> tuple[Conversation, str] | None:
        return self._runtime.process_bound_channel_message(
            channel,
            message,
            binding_key=binding_key,
            user_id=user_id,
            thread_id=thread_id,
            reply_user=reply_user,
            context_token=context_token,
            platform_label=platform_label,
            reply_normalizer=reply_normalizer,
            binding_updates=binding_updates,
        )

    def update_channel_runtime_state(
        self,
        channel: ChannelConfig,
        *,
        config_updates: dict[str, Any] | None = None,
        status: str | None = None,
    ) -> ChannelConfig:
        return self._runtime.update_channel_runtime_state(channel, config_updates=config_updates, status=status)

    def remember_channel_server_handle(self, channel: ChannelConfig, handle: ChannelServerHandle) -> None:
        self._runtime.remember_channel_server_handle(channel, handle)

    # WeChat lifecycle / QR bridge -------------------------------------------------
    def start_wechat_webhook_server(self, channel: ChannelConfig) -> None:
        from core.channel.sources.wechat.webhook_server import start_wechat_webhook_server

        handle = start_wechat_webhook_server(self, channel)
        if handle is not None:
            self.remember_channel_server_handle(channel, handle)

    def start_wechat_bridge_worker(self, channel: ChannelConfig) -> None:
        from core.channel.sources.wechat.bridge_poller import start_wechat_bridge_worker

        start_wechat_bridge_worker(self, channel)

    def get_wechat_bridge_worker(self, channel_id: str) -> _WeChatBridgePollerHandle | None:
        return self._wechat_source.get_bridge_worker(channel_id)

    def remember_wechat_bridge_worker(self, channel_id: str, handle: _WeChatBridgePollerHandle) -> None:
        self._wechat_source.remember_bridge_worker(channel_id, handle)

    def get_wechat_bridge_uin(self, channel_id: str) -> str:
        return self._wechat_source.get_bridge_uin(channel_id)

    def resolve_wechat_bridge_credentials(self, channel: ChannelConfig) -> dict[str, str] | None:
        return self._wechat_source.resolve_bridge_credentials(channel)

    def fetch_wechat_bridge_updates(
        self,
        *,
        base_url: str,
        bot_token: str,
        uin: str,
        cursor: str,
    ) -> dict[str, Any]:
        return self._wechat_source.fetch_bridge_updates(
            base_url=base_url,
            bot_token=bot_token,
            uin=uin,
            cursor=cursor,
        )

    def enqueue_wechat_bridge_message(self, channel: ChannelConfig, raw_message: Any) -> None:
        self._wechat_source.enqueue_bridge_message(self, channel, raw_message)

    def is_wechat_bridge_reauth_required(self, exc: Exception) -> bool:
        return self._wechat_source.is_bridge_reauth_required(exc)

    def is_wechat_bridge_transient_poll_error(self, exc: Exception) -> bool:
        return self._wechat_source.is_bridge_transient_poll_error(exc)

    def coalesce_text(self, mapping: dict[str, Any], *keys: str, default: str = "") -> str:
        return self._wechat_source.coalesce_text(mapping, *keys, default=default)

    def process_wechat_delivery(self, channel: ChannelConfig, inbound: Any) -> None:
        self._wechat_source.process_delivery(self, channel, inbound)

    def build_wechat_qr_snapshot_from_config(self, channel: ChannelConfig) -> ChannelConnectionSnapshot:
        return self._wechat_source.build_qr_snapshot_from_config(channel)

    def normalize_wechat_bridge_base_url(self, value: Any) -> str:
        return self._wechat_source.normalize_bridge_base_url(value)

    def create_wechat_qr_bridge_session(self, channel: ChannelConfig, *, bridge_api_base: str) -> dict[str, Any]:
        return self._wechat_source.create_qr_bridge_session(channel, bridge_api_base=bridge_api_base)

    def fetch_wechat_qr_bridge_session(
        self,
        channel: ChannelConfig,
        *,
        bridge_api_base: str,
        session_id: str,
    ) -> dict[str, Any]:
        return self._wechat_source.fetch_qr_bridge_session(
            channel,
            bridge_api_base=bridge_api_base,
            session_id=session_id,
        )

    def snapshot_from_wechat_qr_payload(
        self,
        channel: ChannelConfig,
        payload: dict[str, Any],
        *,
        fallback: dict[str, Any] | None = None,
    ) -> ChannelConnectionSnapshot:
        return self._wechat_source.snapshot_from_qr_payload(channel, payload, fallback=fallback)

    # Feishu lifecycle -------------------------------------------------------------
    def start_feishu_webhook_server(self, channel: ChannelConfig) -> None:
        from core.channel.sources.feishu.webhook_server import start_feishu_webhook_server

        handle = start_feishu_webhook_server(self, channel)
        if handle is not None:
            self.remember_channel_server_handle(channel, handle)

    def start_feishu_websocket_client(self, channel: ChannelConfig) -> None:
        from core.channel.sources.feishu.ws_client import start_feishu_ws_client

        start_feishu_ws_client(self, channel)

    def get_feishu_ws_worker(self, channel_id: str) -> Any:
        return self._feishu_source.get_ws_worker(channel_id)

    def remember_feishu_ws_worker(self, channel_id: str, handle: Any) -> None:
        self._feishu_source.remember_ws_worker(channel_id, handle)

    # QQBot lifecycle -------------------------------------------------------------
    def start_qqbot_webhook_server(self, channel: ChannelConfig) -> None:
        from core.channel.sources.qqbot.webhook_server import start_qqbot_webhook_server

        handle = start_qqbot_webhook_server(self, channel)
        if handle is not None:
            self.remember_channel_server_handle(channel, handle)

    def start_qqbot_websocket_client(self, channel: ChannelConfig) -> None:
        from core.channel.sources.qqbot.ws_client import start_qqbot_ws_client

        start_qqbot_ws_client(self, channel)

    def get_qqbot_ws_worker(self, channel_id: str) -> Any:
        return self._qqbot_source.get_ws_worker(channel_id)

    def remember_qqbot_ws_worker(self, channel_id: str, handle: Any) -> None:
        self._qqbot_source.remember_ws_worker(channel_id, handle)

    # Telegram lifecycle ---------------------------------------------------------
    def start_telegram_poller(self, channel: ChannelConfig) -> None:
        from core.channel.sources.telegram.poller import start_telegram_poller

        start_telegram_poller(self, channel)

    def get_telegram_poller_worker(self, channel_id: str) -> Any:
        return self._telegram_source.get_poller_worker(channel_id)

    def remember_telegram_poller_worker(self, channel_id: str, handle: Any) -> None:
        self._telegram_source.remember_poller_worker(channel_id, handle)

__all__ = ["ChannelRuntimeContext"]