from __future__ import annotations

import base64
import logging
import random
from typing import Any

import httpx

from core.channel.models import ChannelConnectionSnapshot, _WeChatBridgePollerHandle
from core.channel.sources.message import channel_value
from core.channel.sources.wechat.client import WeChatChannelClient
from core.channel.sources.wechat.protocol import normalize_wechat_reply_text
from core.channel.sources.wechat.router import extract_wechat_bridge_text, normalize_wechat_bridge_message
from core.config.schema import ChannelConfig
from models.conversation import Message


logger = logging.getLogger(__name__)


class WeChatRuntimeSource:
    """WeChat-specific runtime helpers kept outside ``ChannelRuntimeService``."""

    def __init__(self, client: WeChatChannelClient | None = None) -> None:
        self._client = client or WeChatChannelClient()
        self._bridge_uins: dict[str, str] = {}
        self._bridge_workers: dict[str, _WeChatBridgePollerHandle] = {}

    def reset(self) -> None:
        self._bridge_uins.clear()

    def stop(self) -> None:
        for handle in list(self._bridge_workers.values()):
            handle.stop_event.set()
        for handle in list(self._bridge_workers.values()):
            try:
                if handle.thread is not None and handle.thread.is_alive():
                    handle.thread.join(timeout=2.0)
            except Exception as exc:
                logger.debug("Failed to join WeChat bridge thread %s: %s", handle.channel_id, exc)
        self._bridge_workers.clear()
        self.reset()

    def get_bridge_worker(self, channel_id: str) -> _WeChatBridgePollerHandle | None:
        return self._bridge_workers.get(str(channel_id or "").strip())

    def remember_bridge_worker(self, channel_id: str, handle: _WeChatBridgePollerHandle) -> None:
        normalized_id = str(channel_id or "").strip()
        if normalized_id:
            self._bridge_workers[normalized_id] = handle

    def build_qr_snapshot_from_config(self, channel: ChannelConfig) -> ChannelConnectionSnapshot:
        return self._client.build_qr_snapshot_from_config(channel)

    def create_qr_bridge_session(self, channel: ChannelConfig, *, bridge_api_base: str) -> dict[str, Any]:
        if self.wechat_bridge_uses_cherry_protocol(bridge_api_base):
            return self.create_cherry_bridge_session(channel, bridge_api_base=bridge_api_base)
        return self.create_session_api_bridge_session(channel, bridge_api_base=bridge_api_base)

    def create_session_api_bridge_session(self, channel: ChannelConfig, *, bridge_api_base: str) -> dict[str, Any]:
        return self._client.create_session_api_bridge_session(channel, bridge_api_base=bridge_api_base)

    def create_cherry_bridge_session(self, channel: ChannelConfig, *, bridge_api_base: str) -> dict[str, Any]:
        return self._client.create_cherry_bridge_session(channel, bridge_api_base=bridge_api_base)

    def fetch_qr_bridge_session(
        self,
        channel: ChannelConfig,
        *,
        bridge_api_base: str,
        session_id: str,
    ) -> dict[str, Any]:
        if self.wechat_bridge_uses_cherry_protocol(bridge_api_base):
            return self.fetch_cherry_bridge_session(
                channel,
                bridge_api_base=bridge_api_base,
                session_id=session_id,
            )
        return self.fetch_session_api_bridge_session(
            channel,
            bridge_api_base=bridge_api_base,
            session_id=session_id,
        )

    def fetch_session_api_bridge_session(
        self,
        channel: ChannelConfig,
        *,
        bridge_api_base: str,
        session_id: str,
    ) -> dict[str, Any]:
        return self._client.fetch_session_api_bridge_session(
            channel,
            bridge_api_base=bridge_api_base,
            session_id=session_id,
        )

    def fetch_cherry_bridge_session(
        self,
        channel: ChannelConfig,
        *,
        bridge_api_base: str,
        session_id: str,
    ) -> dict[str, Any]:
        return self._client.fetch_cherry_bridge_session(
            channel,
            bridge_api_base=bridge_api_base,
            session_id=session_id,
        )

    def snapshot_from_qr_payload(
        self,
        channel: ChannelConfig,
        payload: dict[str, Any],
        *,
        fallback: dict[str, Any] | None = None,
    ) -> ChannelConnectionSnapshot:
        return self._client.snapshot_from_qr_payload(channel, payload, fallback=fallback)

    def bridge_headers(self, channel: ChannelConfig) -> dict[str, str]:
        return self._client.bridge_headers(channel)

    def get_bridge_uin(self, channel_id: str) -> str:
        normalized_id = str(channel_id or "").strip()
        if not normalized_id:
            return self.build_bridge_uin()
        cached = self._bridge_uins.get(normalized_id)
        if cached:
            return cached
        generated = self.build_bridge_uin()
        self._bridge_uins[normalized_id] = generated
        return generated

    def resolve_bridge_credentials(self, channel: ChannelConfig) -> dict[str, str] | None:
        return self._client.resolve_bridge_credentials(channel)

    def fetch_bridge_updates(
        self,
        *,
        base_url: str,
        bot_token: str,
        uin: str,
        cursor: str,
    ) -> dict[str, Any]:
        return self._client.fetch_bridge_updates(
            base_url=base_url,
            bot_token=bot_token,
            uin=uin,
            cursor=cursor,
        )

    def enqueue_bridge_message(self, context: Any, channel: ChannelConfig, raw_message: Any) -> None:
        envelope = normalize_wechat_bridge_message(
            raw_message,
            mark_recent=lambda message_id: context.mark_recent_message(channel.id, message_id),
        )
        if envelope is None:
            return
        context.enqueue_channel_message(
            channel,
            envelope.content,
            meta=envelope.meta,
        )

    def process_delivery(self, context: Any, channel: ChannelConfig, inbound: Any) -> None:
        context.enqueue_channel_message(
            channel,
            inbound.content,
            meta={
                "user": inbound.from_user,
                "thread_id": inbound.from_user,
                "message_id": inbound.message_id or inbound.dedupe_key,
                "platform": "wechat",
                "to_user": inbound.to_user,
                "msg_type": inbound.msg_type,
                "event": inbound.event,
                "event_key": inbound.event_key,
            },
        )

    def process_message(self, context: Any, channel: ChannelConfig, message: Message) -> None:
        user_id = channel_value(message, "user") or channel_value(message, "thread_id")
        reply_user = (
            channel_value(message, "reply_user")
            or str((channel.config or {}).get("receiver_id", "") or "").strip()
            or user_id
        )
        context_token = channel_value(message, "context_token")
        thread_id = channel_value(message, "thread_id") or user_id
        processed = context.process_bound_channel_message(
            channel,
            message,
            binding_key=user_id or thread_id,
            user_id=user_id,
            thread_id=thread_id,
            reply_user=reply_user,
            context_token=context_token,
            platform_label="WeChat",
            reply_normalizer=normalize_wechat_reply_text,
        )
        if processed is None:
            return
        _, reply_text = processed

        try:
            self.send_reply(
                channel,
                touser=reply_user,
                content=reply_text,
                context_token=context_token,
            )
        except Exception as exc:
            logger.warning("Failed to send WeChat reply for channel %s: %s", getattr(channel, "id", ""), exc)

    def send_reply(self, channel: ChannelConfig, *, touser: str, content: str, context_token: str = "") -> None:
        config = dict(getattr(channel, "config", {}) or {})
        mode = str(config.get("connection_mode", "official-webhook") or "official-webhook").strip().lower() or "official-webhook"
        if mode == "qr-bridge":
            self.send_bridge_reply(channel, touser=touser, content=content, context_token=context_token)
            return

        app_id = str((channel.config or {}).get("app_id", "") or "").strip()
        app_secret = str((channel.config or {}).get("app_secret", "") or "").strip()
        if not (app_id and app_secret and touser):
            raise RuntimeError("微信频道缺少 app_id / app_secret / touser，无法回发消息。")

        access_token = self.get_access_token(channel, app_id=app_id, app_secret=app_secret)
        self._client.send_official_reply(access_token, touser=touser, content=content)

    def send_bridge_reply(
        self,
        channel: ChannelConfig,
        *,
        touser: str,
        content: str,
        context_token: str = "",
    ) -> None:
        self._client.send_bridge_reply(
            channel,
            touser=touser,
            content=content,
            context_token=context_token,
            bridge_uin=self.get_bridge_uin(str(getattr(channel, "id", "") or "")),
        )

    def get_access_token(self, channel: ChannelConfig, *, app_id: str, app_secret: str) -> str:
        return self._client.get_access_token(channel, app_id=app_id, app_secret=app_secret)

    @staticmethod
    def normalize_qr_status(value: Any) -> str:
        return WeChatChannelClient.normalize_qr_status(value)

    @staticmethod
    def wechat_bridge_uses_cherry_protocol(bridge_api_base: str) -> bool:
        return WeChatChannelClient.wechat_bridge_uses_cherry_protocol(bridge_api_base)

    @staticmethod
    def read_bridge_json(response: httpx.Response, *, operation: str) -> dict[str, Any]:
        return WeChatChannelClient.read_bridge_json(response, operation=operation)

    @staticmethod
    def coalesce_text(mapping: dict[str, Any], *keys: str, default: str = "") -> str:
        return WeChatChannelClient.coalesce_text(mapping, *keys, default=default)

    @staticmethod
    def normalize_bridge_base_url(value: Any) -> str:
        return WeChatChannelClient.normalize_bridge_base_url(value)

    @staticmethod
    def build_bridge_base_info() -> dict[str, str]:
        return WeChatChannelClient.build_bridge_base_info()

    @staticmethod
    def build_bridge_uin() -> str:
        random_value = str(random.getrandbits(32))
        return base64.b64encode(random_value.encode("utf-8")).decode("ascii")

    @staticmethod
    def wechat_bridge_bot_headers(bot_token: str, uin: str) -> dict[str, str]:
        return WeChatChannelClient.wechat_bridge_bot_headers(bot_token, uin)

    @staticmethod
    def is_bridge_reauth_required(exc: Exception) -> bool:
        return WeChatChannelClient.is_bridge_reauth_required(exc)

    @staticmethod
    def is_bridge_transient_poll_error(exc: Exception) -> bool:
        return WeChatChannelClient.is_bridge_transient_poll_error(exc)

    @staticmethod
    def extract_bridge_text(item_list: Any) -> str:
        return extract_wechat_bridge_text(item_list)


__all__ = ["WeChatRuntimeSource"]