from __future__ import annotations

from dataclasses import dataclass
import uuid
from typing import Any

from pycat.core.channel.catalog import ChannelCatalog
from pycat.core.channel.sessions import ChannelConversationSummary
from pycat.models.contracts.channel import ChannelConfig
from pycat.models.conversation import Conversation


@dataclass(frozen=True)
class ResolvedChannelConversation:
    conversation: Conversation
    focus_requested: bool = False


class ChannelSessionResolverAdapter:
    """Generic channel conversation routing and binding updates."""

    def __init__(
        self,
        *,
        conv_service: Any,
        bindings: Any,
        ensure_channel_session,
        build_default_conversation,
    ) -> None:
        self._conv_service = conv_service
        self._bindings = bindings
        self._ensure_channel_session = ensure_channel_session
        self._build_default_conversation = build_default_conversation

    def resolve_conversation(
        self,
        channel: ChannelConfig,
        binding_key: str,
        *,
        user_id: str = "",
    ) -> ResolvedChannelConversation:
        normalized_key = str(binding_key or user_id or "").strip()
        bound_session_id = str(getattr(channel, "session_id", "") or "").strip()

        if normalized_key:
            existing_id = self._bindings.get(channel.id, normalized_key)
            if existing_id:
                conversation = self._conv_service.load(existing_id)
                if conversation is not None:
                    return ResolvedChannelConversation(conversation=conversation, focus_requested=False)

            if bound_session_id:
                primary_conversation = self._conv_service.load(bound_session_id)
                if primary_conversation is not None:
                    binding = self._channel_binding(primary_conversation)
                    bound_key = self._bound_key(binding)
                    if bound_key and bound_key == normalized_key:
                        self._bindings.set(channel.id, normalized_key, primary_conversation.id)
                        return ResolvedChannelConversation(conversation=primary_conversation, focus_requested=False)

            conversation = self._build_default_conversation(channel, user_id or normalized_key)
            self._conv_service.save(conversation)
            self._bindings.set(channel.id, normalized_key, conversation.id)
            return ResolvedChannelConversation(conversation=conversation, focus_requested=True)

        if bound_session_id:
            ensured_channel = self._ensure_channel_session(channel)
            conversation = self._conv_service.load(ensured_channel.session_id)
            if conversation is not None:
                return ResolvedChannelConversation(conversation=conversation, focus_requested=False)

        conversation = self._build_default_conversation(channel, user_id or normalized_key)
        self._conv_service.save(conversation)
        return ResolvedChannelConversation(conversation=conversation, focus_requested=False)

    def build_channel_binding(
        self,
        channel: ChannelConfig,
        *,
        existing: dict[str, Any] | None = None,
        user_id: str = "",
        binding_key: str = "",
        manual_session: bool | None = None,
        updates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        binding = dict(existing or {})
        binding["channel_id"] = str(channel.id or "").strip()
        binding["channel_name"] = str(getattr(channel, "name", "") or "").strip()
        binding["source"] = str(channel.source or "").strip()

        if manual_session is not None:
            binding["manual_test_session"] = bool(manual_session)
        else:
            binding["manual_test_session"] = bool(binding.get("manual_test_session", False))

        normalized_user = str(user_id or "").strip()
        normalized_key = str(binding_key or normalized_user or binding.get("binding_key", "") or "").strip()
        if normalized_key:
            binding["binding_key"] = normalized_key
        elif "binding_key" not in binding:
            binding["binding_key"] = ""

        if normalized_user:
            binding["user"] = normalized_user
            binding.setdefault("reply_user", normalized_user)
            binding.setdefault("thread_id", normalized_key or normalized_user)
        elif "user" not in binding:
            binding["user"] = ""

        for key, value in dict(updates or {}).items():
            normalized_field = str(key or "").strip()
            if not normalized_field:
                continue
            if value is None:
                binding.pop(normalized_field, None)
                continue
            if isinstance(value, str):
                binding[normalized_field] = value.strip()
            else:
                binding[normalized_field] = value

        if not str(binding.get("binding_key", "") or "").strip():
            fallback_key = self._bound_key(binding)
            binding["binding_key"] = fallback_key
        return binding

    def remember_binding_context(
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
        resolved_key = str(binding_key or thread_id or user_id or "").strip()
        merged_updates = {
            "thread_id": str(thread_id or "").strip(),
            "reply_user": str(reply_user or "").strip(),
            "context_token": str(context_token or "").strip(),
        }
        merged_updates.update(dict(updates or {}))
        binding = self.build_channel_binding(
            channel,
            existing=self._channel_binding(conversation),
            user_id=user_id,
            binding_key=resolved_key,
            manual_session=None,
            updates=merged_updates,
        )
        self._conv_service.set_setting(conversation, "channel_binding", binding)

    @staticmethod
    def _channel_binding(conversation: Conversation) -> dict[str, Any] | None:
        settings = getattr(conversation, "settings", {}) or {}
        binding = settings.get("channel_binding") if isinstance(settings, dict) else None
        return binding if isinstance(binding, dict) else None

    @staticmethod
    def _bound_key(binding: dict[str, Any] | None) -> str:
        if not isinstance(binding, dict):
            return ""
        return (
            str(binding.get("binding_key", "") or "").strip()
            or str(binding.get("reply_user", "") or "").strip()
            or str(binding.get("thread_id", "") or "").strip()
            or str(binding.get("user", "") or "").strip()
        )


class ChannelSessionResolver:
    """Owns channel conversation creation, binding, and listing."""

    def __init__(
        self,
        *,
        conv_service: Any,
        bindings: Any,
        channel_catalog: ChannelCatalog,
    ) -> None:
        self._conv_service = conv_service
        self._channel_catalog = channel_catalog
        self._adapter = ChannelSessionResolverAdapter(
            conv_service=self._conv_service,
            bindings=bindings,
            ensure_channel_session=lambda channel: self.ensure_channel_session(channel, persist=True),
            build_default_conversation=self._build_default_conversation,
        )

    def ensure_channel_session(self, channel: ChannelConfig, *, persist: bool = True) -> ChannelConfig:
        normalized = self._channel_catalog.ensure_channel(channel)
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
                existing_binding = self._channel_binding(conversation)
                existing_channel_id = str((existing_binding or {}).get("channel_id", "") if isinstance(existing_binding, dict) else "").strip()
                if existing_channel_id and existing_channel_id != str(getattr(normalized, "id", "") or "").strip():
                    raise ValueError("conversation is already bound to another channel")
                self._prepare_channel_conversation(
                    conversation,
                    normalized,
                    user_id="",
                    manual_session=True,
                )
            self._conv_service.save(conversation)

        return self.with_channel_session_id(normalized, session_id)

    def bind_channel_session(
        self,
        channel: ChannelConfig,
        conversation_id: str | None = None,
    ) -> ChannelConfig:
        normalized = self._channel_catalog.ensure_channel(channel)
        target_id = str(conversation_id or "").strip()
        previous_id = str(getattr(normalized, "session_id", "") or "").strip()

        if not target_id:
            return self.ensure_channel_session(normalized, persist=True)

        conversation = self._conv_service.load(target_id)
        if conversation is None:
            raise ValueError(f"conversation not found: {target_id}")

        existing_binding = self._channel_binding(conversation)
        existing_channel_id = str((existing_binding or {}).get("channel_id", "") if isinstance(existing_binding, dict) else "").strip()
        if existing_channel_id and existing_channel_id != str(getattr(normalized, "id", "") or "").strip():
            raise ValueError("conversation is already bound to another channel")

        self._prepare_channel_conversation(
            conversation,
            normalized,
            user_id="",
            manual_session=True,
        )
        self._conv_service.save(conversation)

        if previous_id and previous_id != target_id:
            self._clear_primary_channel_binding(previous_id, normalized)

        return self.with_channel_session_id(normalized, target_id)

    def list_bindable_conversations(self, channel: ChannelConfig) -> tuple[ChannelConversationSummary, ...]:
        normalized = self._channel_catalog.ensure_channel(channel)
        current_channel_id = str(getattr(normalized, "id", "") or "").strip()
        primary_session_id = str(getattr(normalized, "session_id", "") or "").strip()

        summaries: list[ChannelConversationSummary] = []
        for row in self._conv_service.list_all():
            conversation_id = str((row or {}).get("id", "") or "").strip()
            if not conversation_id:
                continue
            conversation = self._conv_service.load(conversation_id)
            if conversation is None:
                continue

            binding = self._channel_binding(conversation)
            bound_channel_id = str((binding or {}).get("channel_id", "") if isinstance(binding, dict) else "").strip()
            bound_channel_name = str((binding or {}).get("channel_name", "") if isinstance(binding, dict) else "").strip()
            is_primary = conversation_id == primary_session_id
            is_other = bool(bound_channel_id and bound_channel_id != current_channel_id)
            participant_label = ""
            is_manual = is_primary
            if isinstance(binding, dict):
                participant_label = (
                    str(binding.get("reply_user", "") or "").strip()
                    or str(binding.get("user", "") or "").strip()
                    or str(binding.get("thread_id", "") or "").strip()
                )
                is_manual = bool(binding.get("manual_test_session", False)) or is_manual

            summaries.append(
                ChannelConversationSummary(
                    conversation_id=conversation_id,
                    title=str(getattr(conversation, "title", "") or conversation_id).strip() or conversation_id,
                    updated_at=self._conversation_updated_at(conversation),
                    preview=conversation_preview(conversation),
                    participant_label=participant_label,
                    is_manual_test_session=is_manual,
                    is_primary_session=is_primary,
                    bound_channel_id=bound_channel_id,
                    bound_channel_name=bound_channel_name,
                    is_bindable=not is_other,
                    is_bound_to_other_channel=is_other,
                )
            )

        summaries.sort(
            key=lambda item: (
                0 if item.is_primary_session else 1,
                1 if item.is_bound_to_other_channel else 0,
                -item.updated_at,
                item.title.lower(),
            )
        )
        return tuple(summaries)

    def list_channel_conversations(self, channel: ChannelConfig) -> tuple[ChannelConversationSummary, ...]:
        normalized = self._channel_catalog.ensure_channel(channel)
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

            summaries.append(
                ChannelConversationSummary(
                    conversation_id=conversation_id,
                    title=str(getattr(conversation, "title", "") or conversation_id).strip() or conversation_id,
                    updated_at=self._conversation_updated_at(conversation),
                    preview=conversation_preview(conversation),
                    participant_label=participant_label,
                    is_manual_test_session=is_manual,
                    is_primary_session=conversation_id == primary_session_id,
                    bound_channel_id=bound_channel_id,
                    bound_channel_name=str((binding or {}).get("channel_name", "") if isinstance(binding, dict) else "").strip(),
                    is_bindable=True,
                    is_bound_to_other_channel=False,
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

    def resolve_conversation(
        self,
        channel: ChannelConfig,
        *,
        binding_key: str,
        user_id: str = "",
    ) -> tuple[Conversation, bool]:
        resolved = self._adapter.resolve_conversation(channel, binding_key, user_id=user_id)
        return resolved.conversation, bool(resolved.focus_requested)

    def build_channel_binding(
        self,
        channel: ChannelConfig,
        *,
        existing: dict[str, Any] | None = None,
        user_id: str = "",
        binding_key: str = "",
        manual_session: bool | None = None,
        updates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._adapter.build_channel_binding(
            channel,
            existing=existing,
            user_id=user_id,
            binding_key=binding_key,
            manual_session=manual_session,
            updates=updates,
        )

    def remember_binding_context(
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
        self._adapter.remember_binding_context(
            conversation,
            channel,
            user_id=user_id,
            thread_id=thread_id,
            reply_user=reply_user,
            context_token=context_token,
            binding_key=binding_key,
            updates=updates,
        )

    def with_channel_session_id(self, channel: ChannelConfig, session_id: str) -> ChannelConfig:
        payload = channel.to_dict()
        payload["session_id"] = str(session_id or "").strip()
        return self._channel_catalog.ensure_channel(ChannelConfig.from_dict(payload))

    def _clear_primary_channel_binding(self, conversation_id: str, channel: ChannelConfig) -> None:
        conversation = self._conv_service.load(conversation_id)
        if conversation is None:
            return
        binding = self._channel_binding(conversation)
        if not isinstance(binding, dict):
            return
        if str(binding.get("channel_id", "") or "").strip() != str(getattr(channel, "id", "") or "").strip():
            return
        self._conv_service.set_setting(conversation, "channel_binding", None)
        self._conv_service.save(conversation)

    @staticmethod
    def _conversation_updated_at(conversation: Conversation) -> float:
        try:
            updated = getattr(conversation, "updated_at", None)
            if updated is not None:
                return float(updated.timestamp())
        except Exception:
            return 0.0
        return 0.0

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
        title = manual_conversation_title(channel) if manual_session else default_conversation_title(channel, user_id)
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
        current_title = str(getattr(conversation, "title", "") or "").strip()
        if manual_session:
            desired_title = manual_conversation_title(channel)
            if not current_title or current_title in {"New Chat", "Imported Chat"} or not list(getattr(conversation, "messages", []) or []):
                self._conv_service.set_title(conversation, desired_title)
        self._conv_service.set_mode(conversation, str(channel.mode_slug or "channel").strip().lower() or "channel")
        existing_settings = getattr(conversation, "settings", {}) or {}
        existing_binding = existing_settings.get("channel_binding") if isinstance(existing_settings, dict) else None
        binding = self.build_channel_binding(
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
                "channel_binding": binding,
            },
        )
        return conversation

    @staticmethod
    def _channel_binding(conversation: Conversation) -> dict[str, Any] | None:
        settings = getattr(conversation, "settings", {}) or {}
        binding = settings.get("channel_binding") if isinstance(settings, dict) else None
        return binding if isinstance(binding, dict) else None

def conversation_preview(conversation: Conversation) -> str:
    messages = list(getattr(conversation, "messages", []) or [])
    for message in reversed(messages):
        content = str(getattr(message, "content", "") or "").strip()
        if content:
            return content.replace("\r\n", " ").replace("\n", " ")[:80]
    return ""


def default_conversation_title(channel: ChannelConfig, user_id: str) -> str:
    suffix = str(user_id or "").strip()
    if suffix:
        suffix = suffix[-8:]
    default_titles = {
        "wechat": "微信频道",
        "feishu": "飞书频道",
        "dingtalk": "钉钉频道",
        "telegram": "Telegram 频道",
        "qqbot": "QQ Bot 频道",
    }
    channel_type = str(getattr(channel, "type", "") or "").strip().lower()
    base = str(getattr(channel, "name", "") or default_titles.get(channel_type, "频道会话")).strip() or default_titles.get(channel_type, "频道会话")
    return f"{base} · {suffix}" if suffix else base


def manual_conversation_title(channel: ChannelConfig) -> str:
    base = str(getattr(channel, "name", "") or "频道测试").strip() or "频道测试"
    return f"{base} · 测试会话"


__all__ = [
    "ChannelSessionResolver",
    "ChannelSessionResolverAdapter",
    "ResolvedChannelConversation",
    "conversation_preview",
    "default_conversation_title",
    "manual_conversation_title",
]
