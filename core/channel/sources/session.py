from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from core.config.schema import ChannelConfig
from models.conversation import Conversation
from services.conversation_service import ConversationService


@dataclass(frozen=True)
class ResolvedChannelConversation:
    conversation: Conversation
    focus_requested: bool = False


class ChannelSessionRuntimeAdapter:
    """Generic channel conversation routing and binding updates."""

    def __init__(
        self,
        *,
        conv_service: ConversationService,
        bindings: Any,
        ensure_channel_session: Callable[[ChannelConfig], ChannelConfig],
        build_default_conversation: Callable[[ChannelConfig, str], Conversation],
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


__all__ = ["ChannelSessionRuntimeAdapter", "ResolvedChannelConversation"]