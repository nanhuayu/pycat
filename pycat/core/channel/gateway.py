from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Any, Callable, Iterable

from pycat.core.app.services.run import RunService
from pycat.core.channel.bindings import ChannelConversationBindingStore
from pycat.core.channel.catalog import ChannelCatalog
from pycat.core.channel.connection import ChannelConnectionHandle, ChannelConnectionSnapshot
from pycat.core.channel.envelope import channel_value
from pycat.core.channel.events import ChannelEvent
from pycat.core.channel.host import ChannelHost
from pycat.core.channel.lifecycle import ChannelLifecycle
from pycat.core.channel.message_processor import ChannelMessageProcessor
from pycat.core.channel.platforms.base import ChannelPlatformBackend
from pycat.core.channel.queue import ChannelQueue
from pycat.core.channel.replies import normalize_reply_text
from pycat.core.channel.scheduler import ChannelTurnScheduler
from pycat.core.channel.session_resolver import ChannelSessionResolver
from pycat.core.channel.sessions import ChannelConversationSummary
from pycat.core.channel.turn_runner import ChannelTurnRunner
from pycat.core.channel.worker import ChannelWorker
from pycat.models.contracts.channel import ChannelConfig
from pycat.models.conversation import Conversation, Message

logger = logging.getLogger(__name__)


class ChannelGateway:
    """Facade for channel connections, inbound dispatch, and runtime events."""

    def __init__(
        self,
        *,
        data_dir: Any,
        channel_catalog: ChannelCatalog,
        platform_backends: Iterable[ChannelPlatformBackend],
        provider_catalog_service: Any,
        conv_service: Any,
        run_service: RunService,
        app_settings_provider: Callable[[], dict[str, Any]],
    ) -> None:
        self._catalog = channel_catalog
        self._conv_service = conv_service
        self._channels_by_id: dict[str, ChannelConfig] = {}
        self._recent_message_ids: OrderedDict[str, None] = OrderedDict()
        self._event_listeners: list[Callable[[ChannelEvent], None]] = []
        self._lock = threading.Lock()
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()

        self._queue = ChannelQueue()
        self._scheduler = ChannelTurnScheduler(max_workers=4)
        self._bindings = ChannelConversationBindingStore(data_dir / "channel_bindings.json")
        self._host = ChannelHost(self)
        self._session_resolver = ChannelSessionResolver(
            conv_service=conv_service,
            bindings=self._bindings,
            channel_catalog=channel_catalog,
        )
        self._turn_runner = ChannelTurnRunner(
            run_service=run_service,
            provider_catalog_service=provider_catalog_service,
            app_settings_provider=app_settings_provider,
            conv_service=conv_service,
            emit_event=self._emit_event,
        )
        self._message_processor = ChannelMessageProcessor(
            conv_service=conv_service,
            session_resolver=self._session_resolver,
            turn_runner=self._turn_runner,
            emit_event=self._emit_event,
            content_service=run_service.content,
        )
        self._worker = ChannelWorker(
            queue=self._queue,
            wake_event=self._wake_event,
            stop_event=self._stop_event,
            process_message=self._schedule_message,
        )
        self._lifecycle = ChannelLifecycle(
            queue=self._queue,
            worker=self._worker,
            scheduler=self._scheduler,
            channel_host=self._host,
            clear_channels=self._clear_channels,
        )
        for backend in tuple(platform_backends or ()):
            self._lifecycle.register_backend(backend)

    def add_event_listener(self, listener: Callable[[ChannelEvent], None]) -> None:
        if listener is None:
            return
        with self._lock:
            if listener not in self._event_listeners:
                self._event_listeners.append(listener)

    def remove_event_listener(self, listener: Callable[[ChannelEvent], None]) -> None:
        with self._lock:
            try:
                self._event_listeners.remove(listener)
            except ValueError:
                pass

    def start(self, channels: Iterable[ChannelConfig]) -> None:
        self.stop()
        normalized = [
            self._catalog.ensure_channel(channel)
            for channel in channels
            if bool(getattr(channel, "enabled", False))
        ]
        self._channels_by_id = {
            channel.id: channel
            for channel in normalized
            if str(channel.id or "").strip()
        }
        self._prune_bindings(valid_channel_ids=set(self._channels_by_id))
        if not normalized:
            return
        self._stop_event.clear()
        try:
            self._lifecycle.start(normalized)
        except Exception:
            # A failed start must not look reconciled to the settings control
            # plane; leave an empty runtime snapshot so the next save retries.
            self.stop()
            raise

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        self._lifecycle.stop()
        self._wake_event.clear()

    @property
    def is_stopping(self) -> bool:
        return self._stop_event.is_set()

    def configured_channels(self) -> tuple[ChannelConfig, ...]:
        """Return the channel configurations currently owned by the gateway."""

        return tuple(self._channels_by_id.values())

    def enqueue_channel_message(
        self,
        channel: ChannelConfig,
        content: str,
        *,
        meta: dict[str, Any] | None = None,
        media=None,
    ) -> None:
        metadata = dict(meta or {})
        metadata["channel_id"] = str(channel.id or "").strip()
        metadata.setdefault("platform", str(channel.type or "channel").strip().lower())
        self._queue.enqueue(channel.source, content, metadata, media=media)
        self._wake_event.set()

    def mark_recent_message(self, channel_id: str, message_key: str) -> bool:
        key = f"{str(channel_id or '').strip()}::{str(message_key or '').strip()}"
        if key.endswith("::"):
            return False
        with self._lock:
            if key in self._recent_message_ids:
                return False
            self._recent_message_ids[key] = None
            while len(self._recent_message_ids) > 512:
                self._recent_message_ids.popitem(last=False)
        return True

    def get_channel(self, channel_id: str) -> ChannelConfig | None:
        return self._channels_by_id.get(str(channel_id or "").strip())

    def remember_channel(self, channel: ChannelConfig) -> None:
        channel_id = str(channel.id or "").strip()
        if channel_id:
            self._channels_by_id[channel_id] = self._catalog.ensure_channel(channel)

    def remember_connection_handle(self, handle: ChannelConnectionHandle) -> None:
        self._lifecycle.remember_connection_handle(handle)

    def report_connection(self, snapshot: ChannelConnectionSnapshot) -> None:
        self._lifecycle.report_connection(snapshot)
        self._emit_event(
            ChannelEvent(
                kind="connection-state",
                channel_id=snapshot.channel_id,
                source="channel-connection",
                payload={"snapshot": snapshot, **snapshot.to_dict()},
            )
        )

    def runtime_connection_snapshot(self, channel_id: str) -> ChannelConnectionSnapshot | None:
        return self._lifecycle.connection_snapshot(channel_id)

    def get_channel_connection_snapshot(self, channel: ChannelConfig) -> ChannelConnectionSnapshot:
        normalized = self._catalog.ensure_channel(channel)
        snapshot = self._lifecycle.connection_snapshot(normalized.id)
        if snapshot is not None:
            return snapshot
        backend = self._lifecycle.get_backend(normalized)
        if backend is not None:
            return backend.connection_snapshot(self._host, normalized)
        return ChannelConnectionSnapshot(
            channel_id=normalized.id,
            channel_type=normalized.type,
            mode=str((normalized.config or {}).get("connection_mode", "") or ""),
            detail="当前频道类型没有可用连接实现。",
        )

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
        reply_normalizer: Callable[[str], str] | None = None,
        binding_updates: dict[str, Any] | None = None,
        reply_sender: Callable[[str, Message | None], None] | None = None,
        attachments=(),
    ) -> tuple[Conversation, str] | None:
        return self._message_processor.process_bound_message(
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
            reply_sender=reply_sender,
            attachments=attachments,
        )

    def ensure_channel_session(self, channel: ChannelConfig, *, persist: bool = True) -> ChannelConfig:
        ensured = self._session_resolver.ensure_channel_session(channel, persist=persist)
        if persist:
            self.remember_channel(ensured)
        return ensured

    def bind_channel_session(self, channel: ChannelConfig, conversation_id: str | None = None) -> ChannelConfig:
        bound = self._session_resolver.bind_channel_session(channel, conversation_id)
        self.remember_channel(bound)
        return bound

    def list_bindable_conversations(self, channel: ChannelConfig) -> tuple[ChannelConversationSummary, ...]:
        return self._session_resolver.list_bindable_conversations(channel)

    def list_channel_conversations(self, channel: ChannelConfig) -> tuple[ChannelConversationSummary, ...]:
        return self._session_resolver.list_channel_conversations(channel)

    def send_bound_conversation_message(self, conversation: Conversation, content: str | Message) -> bool:
        if conversation is None:
            return False
        text = normalize_reply_text(
            getattr(content, "content", content) if isinstance(content, Message) else str(content or ""),
            fallback_text="已收到消息，但暂时没有可发送的文本回复。",
        )
        settings = getattr(conversation, "settings", {}) or {}
        binding = settings.get("channel_binding") if isinstance(settings, dict) else None
        channel_id = str((binding or {}).get("channel_id", "") if isinstance(binding, dict) else "").strip()
        channel = self._channels_by_id.get(channel_id)
        if not (text and channel):
            return False
        backend = self._lifecycle.get_backend(channel)
        if backend is None:
            return False
        reply_user = str(binding.get("reply_user") or binding.get("user") or binding.get("thread_id") or "").strip()
        return backend.send_bound_message(
            self._host,
            channel,
            conversation,
            text=text,
            reply_user=reply_user,
            context_token=str(binding.get("context_token", "") or "").strip(),
        )

    def _schedule_message(self, message: Message) -> None:
        channel_id = channel_value(message, "channel_id")
        channel = self._channels_by_id.get(channel_id)
        if channel is None:
            return
        binding_key = (
            channel_value(message, "binding_key")
            or channel_value(message, "thread_id")
            or channel_value(message, "chat_id")
            or channel_value(message, "user")
            or channel_value(message, "reply_user")
            or channel_value(message, "message_id")
            or "default"
        )
        scheduler_key = f"{channel_id}::{binding_key}"
        self._scheduler.submit(scheduler_key, lambda: self._process_message(channel, message))

    def _process_message(self, channel: ChannelConfig, message: Message) -> None:
        if self.is_stopping:
            return
        backend = self._lifecycle.get_backend(channel)
        if backend is not None:
            backend.process_message(self._host, channel, message)

    def _prune_bindings(self, *, valid_channel_ids: set[str]) -> None:
        try:
            valid_conversation_ids = {
                str((row or {}).get("id", "") or "").strip()
                for row in self._conv_service.list_all()
                if str((row or {}).get("id", "") or "").strip()
            }
            self._bindings.prune(
                valid_channel_ids=valid_channel_ids,
                valid_conversation_ids=valid_conversation_ids,
            )
        except Exception as exc:
            logger.debug("Failed to prune channel bindings: %s", exc)

    def _clear_channels(self) -> None:
        self._channels_by_id.clear()

    def _emit_event(self, event: ChannelEvent) -> None:
        with self._lock:
            listeners = list(self._event_listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception as exc:
                logger.debug("Failed to dispatch channel event %s: %s", event.kind, exc)


__all__ = ["ChannelGateway"]
