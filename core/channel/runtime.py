from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Callable, Optional

from core.channel.models import (
    ChannelServerHandle,
    ChannelConnectionSnapshot,
    ChannelConversationBindingStore,
    ChannelConversationSummary,
    ChannelRuntimeEvent,
)
from core.channel.protocol import ChannelQueue
from core.channel.registry import default_channel_manager
from core.channel.sources import ChannelRuntimeBackend, ChannelRuntimeContext, FeishuChannelBackend, QQBotChannelBackend, TelegramChannelBackend, WeChatChannelBackend
from core.channel.sources.message import channel_value
from core.channel.sources.session import ChannelSessionRuntimeAdapter
from core.channel.sources.feishu.runtime_service import FeishuRuntimeSource
from core.channel.sources.qqbot.runtime_service import QQBotRuntimeSource
from core.channel.sources.telegram.runtime_service import TelegramRuntimeSource
from core.channel.sources.wechat.runtime_service import WeChatRuntimeSource
from core.config.schema import AppConfig, ChannelConfig
from core.runtime.events import TurnEvent, TurnEventKind
from core.runtime.turn_engine import TurnEngine
from core.runtime.turn_policy import TurnPolicy
from core.task.builder import build_run_policy
from core.task.types import TaskStatus
from models.conversation import Conversation, Message
from models.provider import Provider
from services.conversation_service import ConversationService
from services.provider_catalog_service import ProviderCatalogService
from services.storage_service import StorageService


logger = logging.getLogger(__name__)


class ChannelRuntimeService:
    """Background runtime for external channel adapters.

    当前先落地微信：
    - 标准库 HTTP server 接收微信回调
    - 回调消息经 ``ChannelQueue`` 归一化后进入后台 worker
    - worker 选择 / 创建绑定会话，用 ``TurnEngine`` 生成回复
    - 再通过微信客服接口异步回发最终文本
    """

    def __init__(
        self,
        *,
        storage: StorageService,
        provider_catalog_service: ProviderCatalogService,
        conv_service: ConversationService,
        turn_engine: TurnEngine,
    ) -> None:
        self._storage = storage
        self._provider_catalog_service = provider_catalog_service
        self._conv_service = conv_service
        self._turn_engine = turn_engine
        self._channel_manager = default_channel_manager()

        self._queue = ChannelQueue()
        self._bindings = ChannelConversationBindingStore(self._storage.data_dir / "channel_bindings.json")
        self._feishu_source = FeishuRuntimeSource()
        self._qqbot_source = QQBotRuntimeSource()
        self._telegram_source = TelegramRuntimeSource()
        self._wechat_source = WeChatRuntimeSource()
        self._source_context = ChannelRuntimeContext(
            self,
            wechat_source=self._wechat_source,
            feishu_source=self._feishu_source,
            qqbot_source=self._qqbot_source,
            telegram_source=self._telegram_source,
        )
        self._backends: dict[str, ChannelRuntimeBackend] = {}
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._servers: dict[str, ChannelServerHandle] = {}
        self._channels_by_id: dict[str, ChannelConfig] = {}
        self._recent_message_ids: OrderedDict[str, None] = OrderedDict()
        self._event_listeners: list[Callable[[ChannelRuntimeEvent], None]] = []
        self._session_runtime_adapter = ChannelSessionRuntimeAdapter(
            conv_service=self._conv_service,
            bindings=self._bindings,
            ensure_channel_session=lambda channel: self.ensure_channel_session(channel, persist=True),
            build_default_conversation=self._build_default_conversation,
        )
        self._lock = threading.Lock()
        self._register_backend(FeishuChannelBackend(self._feishu_source))
        self._register_backend(QQBotChannelBackend(self._qqbot_source))
        self._register_backend(TelegramChannelBackend(self._telegram_source))
        self._register_backend(WeChatChannelBackend(self._wechat_source))

    def _register_backend(self, backend: ChannelRuntimeBackend) -> None:
        channel_type = str(getattr(backend, "channel_type", "") or "").strip().lower()
        if not channel_type:
            return
        self._backends[channel_type] = backend

    def _get_backend(self, channel: ChannelConfig) -> ChannelRuntimeBackend | None:
        channel_type = str(getattr(channel, "type", "") or "").strip().lower()
        if not channel_type:
            return None
        return self._backends.get(channel_type)

    def add_event_listener(self, listener: Callable[[ChannelRuntimeEvent], None]) -> None:
        if listener is None:
            return
        with self._lock:
            if listener not in self._event_listeners:
                self._event_listeners.append(listener)

    def remove_event_listener(self, listener: Callable[[ChannelRuntimeEvent], None]) -> None:
        with self._lock:
            try:
                self._event_listeners.remove(listener)
            except ValueError:
                return

    def _emit_event(self, event: ChannelRuntimeEvent) -> None:
        with self._lock:
            listeners = list(self._event_listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception as exc:
                logger.debug("Failed to dispatch channel runtime event %s: %s", event.kind, exc)

    def _prune_channel_bindings(self, *, valid_channel_ids: set[str] | None = None) -> None:
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

    def start(self, app_settings: dict[str, Any] | None) -> None:
        self.stop()
        config = AppConfig.from_dict(app_settings or {})
        channels = [
            self._channel_manager.ensure_channel(channel)
            for channel in (getattr(config, "channels", []) or [])
            if bool(getattr(channel, "enabled", False))
        ]
        self._channels_by_id = {
            str(channel.id or "").strip(): channel
            for channel in channels
            if str(channel.id or "").strip()
        }
        self._prune_channel_bindings(valid_channel_ids=set(self._channels_by_id.keys()))
        if not channels:
            return

        self._stop_event.clear()
        self._start_worker_if_needed()

        for channel in channels:
            backend = self._get_backend(channel)
            if backend is None:
                continue
            backend.start(self._source_context, channel)

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

        self._feishu_source.stop()
        self._qqbot_source.stop()
        self._telegram_source.stop()
        self._wechat_source.stop()

        for handle in list(self._servers.values()):
            try:
                handle.httpd.shutdown()
            except Exception as exc:
                logger.debug("Failed to shutdown channel server %s: %s", handle.channel_id, exc)
            try:
                handle.httpd.server_close()
            except Exception as exc:
                logger.debug("Failed to close channel server %s: %s", handle.channel_id, exc)
            try:
                if handle.thread.is_alive():
                    handle.thread.join(timeout=2.0)
            except Exception as exc:
                logger.debug("Failed to join channel server thread %s: %s", handle.channel_id, exc)

        self._servers.clear()
        worker = self._worker_thread
        self._worker_thread = None
        if worker and worker.is_alive():
            worker.join(timeout=2.0)

        self._queue.clear()
        self._channels_by_id.clear()
        self._wake_event.clear()

    def enqueue_channel_message(self, channel: ChannelConfig, content: str, *, meta: dict[str, Any] | None = None) -> None:
        metadata = dict(meta or {})
        metadata["channel_id"] = str(channel.id or "").strip()
        metadata.setdefault("platform", str(channel.type or "channel").strip().lower())
        self._queue.enqueue(channel.source, content, metadata)
        self._wake_event.set()

    def mark_recent_message(self, channel_id: str, message_key: str) -> bool:
        key = f"{str(channel_id or '').strip()}::{str(message_key or '').strip()}"
        if not key or key.endswith("::"):
            return False
        with self._lock:
            if key in self._recent_message_ids:
                return False
            self._recent_message_ids[key] = None
            while len(self._recent_message_ids) > 512:
                self._recent_message_ids.popitem(last=False)
        return True

    @property
    def is_stopping(self) -> bool:
        return bool(self._stop_event.is_set())

    def get_runtime_channel(self, channel_id: str) -> ChannelConfig | None:
        return self._channels_by_id.get(str(channel_id or "").strip())

    def remember_runtime_channel(self, channel: ChannelConfig) -> None:
        channel_id = str(getattr(channel, "id", "") or "").strip()
        if channel_id:
            self._channels_by_id[channel_id] = channel

    def remember_channel_server_handle(self, channel: ChannelConfig, handle: ChannelServerHandle) -> None:
        channel_id = str(getattr(channel, "id", "") or "").strip()
        if channel_id:
            self._servers[channel_id] = handle

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
    ) -> tuple[Conversation, str] | None:
        return self._process_bound_channel_message(
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
        return self._update_channel_runtime_state(channel, config_updates=config_updates, status=status)

    def get_channel_connection_snapshot(self, channel: ChannelConfig) -> ChannelConnectionSnapshot:
        normalized = self._channel_manager.ensure_channel(channel)
        channel_type = str(getattr(normalized, "type", "") or "").strip().lower()
        backend = self._get_backend(normalized)
        if backend is not None:
            return backend.connection_snapshot(self._source_context, normalized)

        config = dict(getattr(normalized, "config", {}) or {})
        mode = str(config.get("connection_mode", "") or "").strip().lower()
        return ChannelConnectionSnapshot(
            channel_id=str(getattr(normalized, "id", "") or ""),
            channel_type=channel_type or "channel",
            mode=mode,
            status=str(getattr(normalized, "status", "draft") or "draft"),
            detail="该频道尚未实现专用连接快照。",
            raw=config,
        )

    def refresh_wechat_connection(self, channel: ChannelConfig, *, force_new: bool = False) -> ChannelConnectionSnapshot:
        normalized = self._channel_manager.ensure_channel(channel)
        channel_type = str(getattr(normalized, "type", "") or "").strip().lower()
        backend = self._get_backend(normalized)
        if channel_type != "wechat" or backend is None:
            return self.get_channel_connection_snapshot(normalized)
        return backend.refresh_connection(self._source_context, normalized, force_new=force_new)

    def ensure_channel_session(self, channel: ChannelConfig, *, persist: bool = True) -> ChannelConfig:
        normalized = self._channel_manager.ensure_channel(channel)
        session_id = str(getattr(normalized, "session_id", "") or "").strip() or str(uuid.uuid4())

        if persist:
            conversation = self._conv_service.load(session_id)
            if conversation is None:
                conversation = self._build_channel_conversation(
                    normalized,
                    user_id="",
                    session_id=session_id,
                    manual_session=True,
                )
            else:
                self._prepare_channel_conversation(
                    conversation,
                    normalized,
                    user_id="",
                    manual_session=True,
                )
            self._conv_service.save(conversation)

        return self._with_channel_session_id(normalized, session_id)

    def list_channel_conversations(self, channel: ChannelConfig) -> tuple[ChannelConversationSummary, ...]:
        normalized = self._channel_manager.ensure_channel(channel)
        channel_id = str(getattr(normalized, "id", "") or "").strip()
        if not channel_id:
            return ()

        primary_session_id = str(getattr(normalized, "session_id", "") or "").strip()
        summaries: list[ChannelConversationSummary] = []

        for row in self._conv_service.list_all():
            conversation_id = str((row or {}).get("id", "") or "").strip()
            if not conversation_id:
                continue

            conversation = self._conv_service.load(conversation_id)
            if conversation is None:
                continue

            settings = getattr(conversation, "settings", {}) or {}
            binding = settings.get("channel_binding") if isinstance(settings, dict) else None
            bound_channel_id = str((binding or {}).get("channel_id", "") if isinstance(binding, dict) else "").strip()
            if conversation_id != primary_session_id and bound_channel_id != channel_id:
                continue

            participant_label = ""
            is_manual = conversation_id == primary_session_id
            if isinstance(binding, dict):
                participant_label = (
                    str(binding.get("reply_user", "") or "").strip()
                    or str(binding.get("user", "") or "").strip()
                    or str(binding.get("thread_id", "") or "").strip()
                )
                is_manual = bool(binding.get("manual_test_session", False)) or is_manual

            updated_at = 0.0
            try:
                updated = getattr(conversation, "updated_at", None)
                if updated is not None:
                    updated_at = float(updated.timestamp())
            except Exception:
                updated_at = 0.0

            summaries.append(
                ChannelConversationSummary(
                    conversation_id=conversation_id,
                    title=str(getattr(conversation, "title", "") or conversation_id).strip() or conversation_id,
                    updated_at=updated_at,
                    preview=self._conversation_preview(conversation),
                    participant_label=participant_label,
                    is_manual_test_session=is_manual,
                    is_primary_session=conversation_id == primary_session_id,
                )
            )

        summaries.sort(
            key=lambda item: (
                0 if item.is_primary_session else 1,
                -item.updated_at,
                item.title.lower(),
            )
        )
        return tuple(summaries)

    def send_bound_conversation_message(self, conversation: Conversation, content: str | Message) -> bool:
        if conversation is None:
            return False

        text = self._normalize_channel_reply_text(
            getattr(content, "content", content) if isinstance(content, Message) else str(content or "")
        )
        if not text:
            return False

        settings = getattr(conversation, "settings", {}) or {}
        binding = settings.get("channel_binding") if isinstance(settings, dict) else None
        if not isinstance(binding, dict):
            return False

        channel_id = str(binding.get("channel_id", "") or "").strip()
        if not channel_id:
            return False

        channel = self._channels_by_id.get(channel_id) or self._load_persisted_channel(channel_id)
        if channel is None:
            return False

        backend = self._get_backend(channel)
        if backend is None:
            return False

        reply_user = str(binding.get("reply_user") or binding.get("user") or binding.get("thread_id") or "").strip()
        context_token = str(binding.get("context_token", "") or "").strip()
        return backend.send_bound_message(
            self._source_context,
            channel,
            conversation,
            text=text,
            reply_user=reply_user,
            context_token=context_token,
        )

    def _start_worker_if_needed(self) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="PyCat-ChannelRuntime",
            daemon=True,
        )
        self._worker_thread.start()

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=0.5)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            for message in self._queue.drain(limit=32):
                try:
                    self._process_message(message)
                except Exception as exc:
                    logger.exception("Channel runtime failed to process inbound message: %s", exc)

    def _load_persisted_channel(self, channel_id: str) -> ChannelConfig | None:
        normalized_id = str(channel_id or "").strip()
        if not normalized_id:
            return None

        config = AppConfig.from_dict(self._storage.load_settings())
        for channel in getattr(config, "channels", []) or []:
            if str(getattr(channel, "id", "") or "").strip() != normalized_id:
                continue
            normalized = self._channel_manager.ensure_channel(channel)
            self._channels_by_id[normalized_id] = normalized
            return normalized
        return None

    def _persist_saved_channel(self, channel: ChannelConfig) -> ChannelConfig:
        normalized = self._channel_manager.ensure_channel(channel)
        channel_id = str(getattr(normalized, "id", "") or "").strip()
        if channel_id:
            self._channels_by_id[channel_id] = normalized

        payload = self._storage.load_settings()
        config = AppConfig.from_dict(payload)
        channels = list(getattr(config, "channels", []) or [])
        updated_channels: list[ChannelConfig] = []
        found = False

        for existing in channels:
            existing_id = str(getattr(existing, "id", "") or "").strip()
            if existing_id == channel_id and channel_id:
                updated_channels.append(normalized)
                found = True
            else:
                updated_channels.append(existing)

        if not found:
            return normalized

        next_payload = config.to_dict()
        next_payload["channels"] = [item.to_dict() for item in updated_channels]
        self._storage.save_settings(next_payload)
        return normalized

    def _update_channel_runtime_state(
        self,
        channel: ChannelConfig,
        *,
        config_updates: dict[str, Any] | None = None,
        status: str | None = None,
    ) -> ChannelConfig:
        payload = channel.to_dict()
        config = dict(payload.get("config", {}) or {})
        if config_updates:
            config.update(dict(config_updates))
        payload["config"] = config
        payload["updated_at"] = int(time.time())
        if status is not None:
            payload["status"] = str(status or "draft").strip() or "draft"
        return self._persist_saved_channel(ChannelConfig.from_dict(payload))

    def _process_message(self, message: Message) -> None:
        channel_id = channel_value(message, "channel_id")
        if not channel_id:
            return
        channel = self._channels_by_id.get(channel_id)
        if channel is None:
            return

        backend = self._get_backend(channel)
        if backend is not None:
            backend.process_message(self._source_context, channel, message)

    def _process_bound_channel_message(
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
    ) -> tuple[Conversation, str] | None:
        inbound_text = str(getattr(message, "content", "") or "").strip()
        resolved_binding_key = str(binding_key or thread_id or reply_user or user_id or "").strip()
        if not (resolved_binding_key and reply_user and inbound_text):
            return None

        conversation, focus_requested = self._resolve_channel_conversation(
            channel,
            binding_key=resolved_binding_key,
            user_id=user_id,
        )
        self._remember_channel_binding_context(
            conversation,
            channel,
            user_id=user_id,
            thread_id=thread_id,
            reply_user=reply_user,
            context_token=context_token,
            binding_key=resolved_binding_key,
            updates=binding_updates,
        )
        conversation.add_message(message)
        self._conv_service.save(conversation)
        self._emit_event(
            ChannelRuntimeEvent(
                kind="conversation-updated",
                channel_id=str(channel.id or "").strip(),
                conversation_id=str(getattr(conversation, "id", "") or "").strip(),
                source="channel-inbound",
                focus_requested=bool(focus_requested),
            )
        )

        request_id = str(uuid.uuid4())
        self._emit_event(
            ChannelRuntimeEvent(
                kind="turn-started",
                channel_id=str(channel.id or "").strip(),
                conversation_id=str(getattr(conversation, "id", "") or "").strip(),
                source="channel-turn",
                request_id=request_id,
                payload={"platform": platform_label, "binding_key": resolved_binding_key},
            )
        )

        reply_text = ""
        final_message: Message | None = None
        try:
            provider = self._resolve_provider(conversation)
            if provider is None:
                raise RuntimeError("当前未找到可用服务商，请先在设置里配置 Provider 和默认模型。")

            policy = self._build_turn_policy(conversation, channel)
            result = self._run_turn(
                provider=provider,
                conversation=conversation,
                policy=policy,
                channel=channel,
                request_id=request_id,
            )

            if result.status == TaskStatus.COMPLETED:
                final_message = result.final_message
                if final_message is not None:
                    metadata = dict(getattr(final_message, "metadata", {}) or {})
                    metadata["channel_runtime_owned"] = True
                    final_message.metadata = metadata
                if final_message is not None and not self._conversation_has_message(conversation, final_message):
                    conversation.add_message(final_message)
                reply_text = self._normalize_channel_reply_text(
                    getattr(final_message, "content", "") if final_message is not None else "",
                    normalizer=reply_normalizer,
                )
            elif result.status == TaskStatus.CANCELLED:
                reply_text = self._normalize_channel_reply_text("消息已收到，但处理过程被取消。", normalizer=reply_normalizer)
                self._emit_event(
                    ChannelRuntimeEvent(
                        kind="turn-error",
                        channel_id=str(channel.id or "").strip(),
                        conversation_id=str(getattr(conversation, "id", "") or "").strip(),
                        source="channel-cancelled",
                        request_id=request_id,
                        payload={"error": reply_text},
                    )
                )
            else:
                reply_text = self._normalize_channel_reply_text(
                    result.error or "消息已收到，但生成回复时失败。",
                    normalizer=reply_normalizer,
                )
                conversation.add_message(
                    Message(
                        role="assistant",
                        content=reply_text,
                        metadata={"channel_runtime_error": True, "channel_id": channel.id},
                    )
                )
                self._emit_event(
                    ChannelRuntimeEvent(
                        kind="turn-error",
                        channel_id=str(channel.id or "").strip(),
                        conversation_id=str(getattr(conversation, "id", "") or "").strip(),
                        source="channel-failed",
                        request_id=request_id,
                        payload={"error": reply_text},
                    )
                )
        except Exception as exc:
            logger.exception("%s channel execution failed: %s", platform_label, exc)
            reply_text = self._normalize_channel_reply_text(
                f"消息已收到，但当前处理失败：{exc}",
                normalizer=reply_normalizer,
            )
            conversation.add_message(
                Message(
                    role="assistant",
                    content=reply_text,
                    metadata={"channel_runtime_error": True, "channel_id": channel.id},
                )
            )
            self._emit_event(
                ChannelRuntimeEvent(
                    kind="turn-error",
                    channel_id=str(channel.id or "").strip(),
                    conversation_id=str(getattr(conversation, "id", "") or "").strip(),
                    source="channel-exception",
                    request_id=request_id,
                    payload={"error": reply_text},
                )
            )
        finally:
            self._conv_service.save(conversation)

        self._emit_event(
            ChannelRuntimeEvent(
                kind="turn-complete",
                channel_id=str(channel.id or "").strip(),
                conversation_id=str(getattr(conversation, "id", "") or "").strip(),
                source="channel-response",
                focus_requested=False,
                request_id=request_id,
                payload={"reply_text": reply_text, "message": final_message},
            )
        )
        self._emit_event(
            ChannelRuntimeEvent(
                kind="conversation-updated",
                channel_id=str(channel.id or "").strip(),
                conversation_id=str(getattr(conversation, "id", "") or "").strip(),
                source="channel-response",
                focus_requested=False,
                request_id=request_id,
            )
        )
        return conversation, reply_text

    @staticmethod
    def _conversation_has_message(conversation: Conversation, message: Message) -> bool:
        message_id = str(getattr(message, "id", "") or "").strip()
        message_seq = getattr(message, "seq_id", None)
        for existing in list(getattr(conversation, "messages", []) or []):
            if message_id and str(getattr(existing, "id", "") or "").strip() == message_id:
                return True
            if message_seq and getattr(existing, "seq_id", None) == message_seq:
                return True
        return False

    @staticmethod
    def _normalize_channel_reply_text(content: Any, *, normalizer: Callable[[str], str] | None = None) -> str:
        raw = str(content or "")
        if callable(normalizer):
            text = str(normalizer(raw) or "").strip()
            if text:
                return text
        fallback = raw.replace("\r\n", "\n").strip()
        return fallback or "已收到消息，但暂时没有可发送的文本回复。"

    def _resolve_conversation(self, channel: ChannelConfig, user_id: str) -> tuple[Conversation, bool]:
        return self._resolve_channel_conversation(channel, binding_key=user_id, user_id=user_id)

    def _resolve_channel_conversation(
        self,
        channel: ChannelConfig,
        *,
        binding_key: str,
        user_id: str = "",
    ) -> tuple[Conversation, bool]:
        resolved = self._session_runtime_adapter.resolve_conversation(channel, binding_key, user_id=user_id)
        return resolved.conversation, bool(resolved.focus_requested)

    def _build_default_conversation(self, channel: ChannelConfig, user_id: str) -> Conversation:
        return self._build_channel_conversation(
            channel,
            user_id=user_id,
            manual_session=not bool(str(user_id or "").strip()),
        )

    def _build_channel_conversation(
        self,
        channel: ChannelConfig,
        user_id: str,
        *,
        session_id: str = "",
        manual_session: bool = False,
    ) -> Conversation:
        title = self._manual_conversation_title(channel) if manual_session else self._default_conversation_title(channel, user_id)
        conversation = self._conv_service.create(title=title)
        if session_id:
            conversation.id = str(session_id or "").strip() or conversation.id
        self._prepare_channel_conversation(
            conversation,
            channel,
            user_id=user_id,
            manual_session=manual_session,
        )
        return conversation

    def _prepare_channel_conversation(
        self,
        conversation: Conversation,
        channel: ChannelConfig,
        *,
        user_id: str,
        manual_session: bool,
    ) -> Conversation:
        provider = self._select_default_provider()
        allow_tools = bool(getattr(channel, "allow_tools", True))
        current_title = str(getattr(conversation, "title", "") or "").strip()
        if manual_session:
            desired_title = self._manual_conversation_title(channel)
            if not current_title or current_title in {"New Chat", "Imported Chat"} or not list(getattr(conversation, "messages", []) or []):
                self._conv_service.set_title(conversation, desired_title)
        if provider is not None:
            self._conv_service.configure_llm(
                conversation,
                providers=[provider],
                provider_id=provider.id,
                provider_name=provider.name,
                api_type=provider.api_type,
                model=str(getattr(provider, "default_model", "") or "").strip()
                or (provider.models[0] if getattr(provider, "models", []) else ""),
            )
        self._conv_service.set_mode(conversation, "agent")
        existing_settings = getattr(conversation, "settings", {}) or {}
        existing_binding = existing_settings.get("channel_binding") if isinstance(existing_settings, dict) else None
        binding = self._build_channel_binding(
            channel,
            existing=existing_binding if isinstance(existing_binding, dict) else None,
            user_id=user_id,
            binding_key=user_id,
            manual_session=manual_session,
        )
        self._conv_service.set_settings(
            conversation,
            {
                "show_thinking": False,
                "enable_mcp": allow_tools,
                "enable_search": allow_tools,
                "channel_binding": binding,
            },
        )
        self._resolve_provider(conversation)
        return conversation

    def _build_channel_binding(
        self,
        channel: ChannelConfig,
        *,
        existing: dict[str, Any] | None = None,
        user_id: str = "",
        binding_key: str = "",
        manual_session: bool | None = None,
        updates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._session_runtime_adapter.build_channel_binding(
            channel,
            existing=existing,
            user_id=user_id,
            binding_key=binding_key,
            manual_session=manual_session,
            updates=updates,
        )

    def _remember_channel_binding_context(
        self,
        conversation: Conversation,
        channel: ChannelConfig,
        *,
        user_id: str,
        thread_id: str,
        reply_user: str,
        context_token: str,
        binding_key: str = "",
        updates: dict[str, Any] | None = None,
    ) -> None:
        self._session_runtime_adapter.remember_binding_context(
            conversation,
            channel,
            user_id=user_id,
            thread_id=thread_id,
            reply_user=reply_user,
            context_token=context_token,
            binding_key=binding_key,
            updates=updates,
        )

    def _with_channel_session_id(self, channel: ChannelConfig, session_id: str) -> ChannelConfig:
        payload = channel.to_dict()
        payload["session_id"] = str(session_id or "").strip()
        return self._channel_manager.ensure_channel(ChannelConfig.from_dict(payload))

    @staticmethod
    def _conversation_preview(conversation: Conversation) -> str:
        messages = list(getattr(conversation, "messages", []) or [])
        for message in reversed(messages):
            content = str(getattr(message, "content", "") or "").strip()
            if content:
                return content.replace("\r\n", " ").replace("\n", " ")[:80]
        return ""

    def _resolve_provider(self, conversation: Conversation) -> Provider | None:
        providers = [provider for provider in self._provider_catalog_service.load() if bool(getattr(provider, "enabled", True))]
        provider = self._conv_service.resolve_provider(
            providers,
            provider_id=str(getattr(conversation, "provider_id", "") or ""),
            provider_name=str(getattr(conversation, "provider_name", "") or ""),
        )
        if provider is None:
            provider = self._select_default_provider(providers)
            if provider is not None:
                self._conv_service.configure_llm(
                    conversation,
                    providers=providers,
                    provider_id=provider.id,
                    provider_name=provider.name,
                    api_type=provider.api_type,
                    model=str(getattr(provider, "default_model", "") or "").strip()
                    or (provider.models[0] if getattr(provider, "models", []) else ""),
                )
        if provider is None:
            return None

        current_model = str(getattr(conversation, "model", "") or "").strip()
        if not current_model:
            fallback_model = str(getattr(provider, "default_model", "") or "").strip()
            if not fallback_model and getattr(provider, "models", None):
                fallback_model = str(provider.models[0] or "").strip()
            if fallback_model:
                self._conv_service.configure_llm(
                    conversation,
                    providers=providers,
                    provider_id=provider.id,
                    provider_name=provider.name,
                    api_type=provider.api_type,
                    model=fallback_model,
                )
        return provider

    def _select_default_provider(self, providers: Optional[list[Provider]] = None) -> Provider | None:
        provider_list = providers if providers is not None else self._provider_catalog_service.load()
        enabled = [provider for provider in provider_list if bool(getattr(provider, "enabled", True))]
        if not enabled:
            return None
        for provider in enabled:
            if str(getattr(provider, "default_model", "") or "").strip():
                return provider
        return enabled[0]

    def _build_turn_policy(self, conversation: Conversation, channel: ChannelConfig) -> TurnPolicy:
        settings = getattr(conversation, "settings", {}) or {}
        mode_slug = str(getattr(conversation, "mode", "") or "").strip().lower()
        if not mode_slug:
            mode_slug = "agent" if bool(getattr(channel, "allow_tools", True)) else "channel"
        policy = build_run_policy(
            mode_slug=mode_slug,
            enable_thinking=bool(settings.get("show_thinking", False)),
            enable_search=bool(settings.get("enable_search", False)),
            enable_mcp=bool(settings.get("enable_mcp", False)),
        )
        return TurnPolicy.from_run_policy(policy, conversation=conversation)

    def _run_turn(
        self,
        *,
        provider: Provider,
        conversation: Conversation,
        policy: TurnPolicy,
        channel: ChannelConfig,
        request_id: str = "",
    ):
        permission_mode = str(getattr(channel, "permission_mode", "") or "default").strip().lower() or "default"
        channel_id = str(getattr(channel, "id", "") or "").strip()
        conversation_id = str(getattr(conversation, "id", "") or "").strip()
        request_token = str(request_id or "").strip() or str(uuid.uuid4())

        async def _approval_callback(_message: str) -> bool:
            return permission_mode == "auto"

        async def _questions_callback(_question: dict) -> dict:
            return {"selected": [], "freeText": None, "skipped": True}

        def _emit_turn_event(kind: str, *, payload: dict[str, Any] | None = None, source: str = "channel-turn") -> None:
            self._emit_event(
                ChannelRuntimeEvent(
                    kind=kind,
                    channel_id=channel_id,
                    conversation_id=conversation_id,
                    source=source,
                    request_id=request_token,
                    payload=dict(payload or {}),
                )
            )

        def _message_payload(message: Message) -> dict[str, Any]:
            metadata = getattr(message, "metadata", {}) or {}
            return {
                "message": message,
                "message_id": str(getattr(message, "id", "") or ""),
                "role": str(getattr(message, "role", "") or ""),
                "seq_id": int(getattr(message, "seq_id", 0) or 0),
                "tool_call_id": str(getattr(message, "tool_call_id", "") or ""),
                "tool_name": str(metadata.get("name", "") or "") if isinstance(metadata, dict) else "",
            }

        def _on_token(token: str) -> None:
            _emit_turn_event("turn-token", payload={"token": str(token or "")})

        def _on_thinking(thinking: str) -> None:
            _emit_turn_event("turn-thinking", payload={"thinking": str(thinking or "")})

        def _on_event(event: TurnEvent) -> None:
            kind_value = getattr(getattr(event, "kind", ""), "value", str(getattr(event, "kind", "")))
            payload: dict[str, Any] = {
                "event": event,
                "event_kind": kind_value,
                "turn": int(getattr(event, "turn", 0) or 0),
                "detail": str(getattr(event, "detail", "") or ""),
            }
            data = getattr(event, "data", None)
            if isinstance(data, Message):
                payload.update(_message_payload(data))
                if event.kind == TurnEventKind.STEP:
                    self._conv_service.save(conversation)
                    _emit_turn_event("turn-step", payload=payload)
            elif isinstance(data, dict):
                payload.update(data)
            elif data is not None:
                payload["data"] = data
            _emit_turn_event("turn-event", payload=payload)

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(
                self._turn_engine.run(
                    provider=provider,
                    conversation=conversation,
                    policy=policy,
                    on_event=_on_event,
                    on_token=_on_token,
                    on_thinking=_on_thinking,
                    approval_callback=_approval_callback,
                    questions_callback=_questions_callback,
                )
            )
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            try:
                loop.run_until_complete(loop.shutdown_default_executor())
            except Exception:
                pass
            loop.close()

    @staticmethod
    def _default_conversation_title(channel: ChannelConfig, user_id: str) -> str:
        suffix = str(user_id or "").strip()
        if suffix:
            suffix = suffix[-8:]
        default_titles = {
            "wechat": "微信频道",
            "feishu": "飞书频道",
            "telegram": "Telegram 频道",
            "qqbot": "QQ Bot 频道",
        }
        channel_type = str(getattr(channel, "type", "") or "").strip().lower()
        base = str(getattr(channel, "name", "") or default_titles.get(channel_type, "频道会话")).strip() or default_titles.get(channel_type, "频道会话")
        return f"{base} · {suffix}" if suffix else base

    @staticmethod
    def _manual_conversation_title(channel: ChannelConfig) -> str:
        base = str(getattr(channel, "name", "") or "频道测试").strip() or "频道测试"
        return f"{base} · 测试会话"