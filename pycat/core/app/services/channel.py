from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable

from pycat.core.channel.catalog import ChannelCatalog
from pycat.core.channel.connection import (
    ChannelConnectionSnapshot,
    ChannelConnectionState,
    ChannelRequiredAction,
)
from pycat.models.contracts.channel import ChannelConfig
from pycat.models.contracts.config import AppConfig


@dataclass(frozen=True)
class ChannelSaveResult:
    channels: tuple[ChannelConfig, ...]
    preferred_session_id: str = ""


class ChannelService:
    """Application use cases for creating, validating, and binding channels."""

    def __init__(
        self,
        *,
        channel_catalog: ChannelCatalog,
        channel_gateway: Any,
        wechat_login_flow: Any | None = None,
        configured_channels_provider: Callable[[], Iterable[ChannelConfig]] | None = None,
    ) -> None:
        self._catalog = channel_catalog
        self._gateway = channel_gateway
        self._wechat_login_flow = wechat_login_flow
        self._configured_channels_provider = configured_channels_provider or (lambda: ())
        self._login_sessions = {}
        self._login_lock = threading.RLock()

    @property
    def catalog(self) -> ChannelCatalog:
        return self._catalog

    def create(self, channel_type: str) -> ChannelConfig:
        definition = self._catalog.get_definition(channel_type)
        channel_id = uuid.uuid4().hex[:12]
        same_type = [
            channel
            for channel in self._configured_channels()
            if str(channel.type or "").strip().lower() == definition.type
        ]
        base_name = definition.default_name or definition.name
        name = f"{base_name} {len(same_type) + 1}"
        config = dict(definition.default_config or {})
        if definition.type == "wechat":
            config["connection_mode"] = "ilink"
        elif definition.type in {"feishu", "qqbot"}:
            config["connection_mode"] = "websocket"
        elif definition.type == "telegram":
            config["connection_mode"] = "polling"
        if definition.type in {"wechat", "feishu", "qqbot"}:
            config["callback_path"] = f"/{definition.type}/{channel_id}"
        return self._catalog.ensure_channel(
            ChannelConfig(
                id=channel_id,
                name=name,
                type=definition.type,
                enabled=True,
                source=f"channel:{definition.type}:{channel_id}",
                mode_slug="channel",
                config=config,
            )
        )

    def prepare_for_save(
        self,
        channels: Iterable[ChannelConfig | dict[str, Any]],
        preferred_session_id: str = "",
    ) -> ChannelSaveResult:
        prepared: list[ChannelConfig] = []
        focus_session_id = str(preferred_session_id or "").strip()
        for item in channels:
            channel = item if isinstance(item, ChannelConfig) else ChannelConfig.from_dict(item)
            channel = self._catalog.ensure_channel(channel)
            errors = self.validation_errors(channel)
            if channel.enabled and errors:
                raise ValueError(f"{channel.name or channel.type}: {'；'.join(errors)}")
            if not str(channel.session_id or "").strip():
                channel = self.bind_session(channel, None)
                if not focus_session_id:
                    focus_session_id = str(channel.session_id or "").strip()
            prepared.append(channel)
        return ChannelSaveResult(tuple(prepared), focus_session_id)

    def set_enabled(self, channel: ChannelConfig, enabled: bool) -> ChannelConfig:
        normalized = self._catalog.ensure_channel(channel)
        if enabled:
            errors = self.validation_errors(normalized)
            if errors:
                raise ValueError("；".join(errors))
        return replace(normalized, enabled=bool(enabled))

    def bind_session(self, channel: ChannelConfig, conversation_id: str | None) -> ChannelConfig:
        return self._gateway.bind_channel_session(self._catalog.ensure_channel(channel), conversation_id)

    def list_bindable_conversations(self, channel: ChannelConfig):
        return self._gateway.list_bindable_conversations(self._catalog.ensure_channel(channel))

    def connection_snapshot(self, channel: ChannelConfig) -> ChannelConnectionSnapshot:
        normalized = self._catalog.ensure_channel(channel)
        if not normalized.enabled:
            return ChannelConnectionSnapshot(
                channel_id=normalized.id,
                channel_type=normalized.type,
                mode=self.connection_mode(normalized),
                state=ChannelConnectionState.DISABLED,
                detail="频道已停用。",
            )
        errors = self.validation_errors(normalized)
        if errors:
            return ChannelConnectionSnapshot(
                channel_id=normalized.id,
                channel_type=normalized.type,
                mode=self.connection_mode(normalized),
                state=ChannelConnectionState.INCOMPLETE,
                required_action=ChannelRequiredAction.RETRY,
                detail="；".join(errors),
            )
        return self._gateway.get_channel_connection_snapshot(normalized)

    def begin_wechat_login(self, channel: ChannelConfig):
        if self._wechat_login_flow is None:
            raise RuntimeError("微信登录组件尚未装配。")
        normalized = self._catalog.ensure_channel(channel)
        session = self._wechat_login_flow.start(
            normalized,
            local_tokens=self._local_wechat_tokens(),
        )
        self._report_login_session(session)
        return session

    def start_client_login(self, channel_id):
        with self._login_lock:
            self._login_sessions = {key: value for key, value in self._login_sessions.items()
                if time.monotonic() - value.started_at < 300}
            if len(self._login_sessions) >= 16:
                raise ValueError('Too many pending channel logins.')
            channel = next((item for item in self._configured_channels() if item.id == channel_id), None)
            if channel is None or channel.type != 'wechat':
                raise ValueError('Select a saved WeChat channel first.')
            session = self.begin_wechat_login(channel)
            self._login_sessions[session.id] = session
            return session

    def client_login(self, identity):
        with self._login_lock:
            session = self._login_sessions.get(identity)
            if session is None or time.monotonic() - session.started_at >= 300:
                self._login_sessions.pop(identity, None)
                raise ValueError('Channel login expired; start again.')
            return session

    def advance_client_login(self, identity, verification_code=''):
        with self._login_lock:
            session = self.client_login(identity)
            if not session.is_complete:
                session = self.poll_wechat_login(session, verification_code)
                self._login_sessions[identity] = session
            return session

    def poll_wechat_login(self, session: Any, verification_code: str = ""):
        if self._wechat_login_flow is None:
            raise RuntimeError("微信登录组件尚未装配。")
        updated = self._wechat_login_flow.poll(session, verification_code=verification_code)
        channel = getattr(updated, "channel", None)
        snapshot = getattr(updated, "snapshot", None)
        if isinstance(channel, ChannelConfig) and isinstance(snapshot, ChannelConnectionSnapshot) and snapshot.is_ready:
            if not str(channel.session_id or "").strip():
                channel = self.bind_session(channel, None)
                updated = replace(updated, channel=channel)
            self._gateway.remember_channel(channel)
        self._report_login_session(updated)
        return updated

    def runtime_channels(self, settings: dict[str, Any] | Iterable[ChannelConfig]) -> tuple[ChannelConfig, ...]:
        if isinstance(settings, dict):

            channels = AppConfig.from_dict(settings).channels
        else:
            channels = list(settings or ())
        return tuple(
            self._catalog.ensure_channel(channel)
            for channel in channels
            if bool(getattr(channel, "enabled", False))
        )

    def validation_errors(self, channel: ChannelConfig) -> tuple[str, ...]:
        normalized = self._catalog.ensure_channel(channel)
        errors = list(self._catalog.validate(normalized))
        if normalized.type == "wechat" and self.connection_mode(normalized) == "ilink":
            config = dict(normalized.config or {})
            if not str(config.get("ilink_token", "") or "").strip():
                errors.append("请先完成个人微信扫码连接")
            if not str(config.get("ilink_bot_id", "") or "").strip():
                errors.append("微信扫码连接尚未返回机器人账号")
        return tuple(dict.fromkeys(errors))

    @staticmethod
    def connection_mode(channel: ChannelConfig) -> str:
        config = dict(getattr(channel, "config", {}) or {})
        return str(config.get("connection_mode", "") or "").strip().lower()

    def _configured_channels(self) -> tuple[ChannelConfig, ...]:
        try:
            return tuple(self._configured_channels_provider() or ())
        except Exception:
            return ()

    def _local_wechat_tokens(self) -> tuple[str, ...]:
        tokens: list[str] = []
        for channel in reversed(self._configured_channels()):
            if str(getattr(channel, "type", "") or "").strip().lower() != "wechat":
                continue
            token = str((getattr(channel, "config", {}) or {}).get("ilink_token", "") or "").strip()
            if token and token not in tokens:
                tokens.append(token)
            if len(tokens) >= 10:
                break
        return tuple(tokens)

    def _report_login_session(self, session: Any) -> None:
        snapshot = getattr(session, "snapshot", None)
        if isinstance(snapshot, ChannelConnectionSnapshot):
            self._gateway.report_connection(snapshot)


__all__ = ["ChannelSaveResult", "ChannelService"]
