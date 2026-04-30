"""Conversation lifecycle service.

Centralizes conversation CRUD and message operations that were
previously scattered across UI presenters and MainWindow.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.app.state import ConversationSettingsUpdate
from models.conversation import Conversation, Message
from models.provider import build_model_ref, provider_matches_name
from services.storage_service import StorageService

logger = logging.getLogger(__name__)


class ConversationService:
    """Manages conversation persistence and message operations."""

    def __init__(self, storage: StorageService) -> None:
        self._storage = storage

    # ------------------------------------------------------------------
    # Conversation CRUD
    # ------------------------------------------------------------------

    def create(self, title: str | None = None) -> Conversation:
        conv = Conversation()
        if title:
            conv.title = title
        return conv

    def load(self, conversation_id: str) -> Optional[Conversation]:
        return self._storage.load_conversation(conversation_id)

    def list_all(self) -> List[Dict[str, Any]]:
        return self._storage.list_conversations()

    def save(self, conversation: Conversation) -> bool:
        return self._storage.save_conversation(conversation)

    def delete(self, conversation_id: str) -> bool:
        return self._storage.delete_conversation(conversation_id)

    def import_from_file(self, file_path: str) -> Optional[Conversation]:
        return self._storage.import_conversation(file_path)

    def duplicate(self, source: Conversation) -> Conversation:
        """Create a deep copy with a new ID and timestamp."""
        dup = Conversation.from_dict(source.to_dict())
        dup.id = str(uuid.uuid4())
        now = datetime.now()
        dup.created_at = now
        dup.updated_at = now
        base_title = (source.title or "New Chat").strip() or "New Chat"
        dup.title = f"{base_title}（副本）"
        return dup

    # ------------------------------------------------------------------
    # Message operations
    # ------------------------------------------------------------------

    def add_message(
        self, conversation: Conversation, message: Message, *, auto_save: bool = False,
    ) -> None:
        conversation.add_message(message)
        if auto_save:
            self.save(conversation)

    def delete_messages(self, conversation: Conversation, message_id: str) -> list[str]:
        return conversation.delete_message(message_id) or []

    def find_message(self, conversation: Conversation, message_id: str) -> Optional[Message]:
        for msg in conversation.messages:
            if msg.id == message_id:
                return msg
        return None

    def ensure_title(self, conversation: Conversation) -> None:
        """Auto-generate a title from the first message if needed."""
        if len(conversation.messages) == 1:
            conversation.generate_title_from_first_message()

    # ------------------------------------------------------------------
    # Provider helpers
    # ------------------------------------------------------------------

    @staticmethod
    def find_provider(providers: list, provider_id: str):
        for p in providers:
            if p.id == provider_id:
                return p
        return None

    @staticmethod
    def resolve_provider(providers: list, provider_id: str = "", provider_name: str = ""):
        normalized_id = str(provider_id or "").strip()
        normalized_name = str(provider_name or "").strip()

        if normalized_id:
            provider = ConversationService.find_provider(providers, normalized_id)
            if provider is not None:
                return provider

        if normalized_name:
            for provider in providers:
                if provider_matches_name(provider, normalized_name):
                    return provider
        return None

    def configure_llm(
        self,
        conversation: Conversation,
        *,
        providers: list | None = None,
        provider_id: str | None = None,
        provider_name: str | None = None,
        api_type: str | None = None,
        model: str | None = None,
    ) -> Conversation:
        provider_list = list(providers or [])
        llm_config = conversation.get_llm_config()

        next_provider_id = llm_config.provider_id
        if provider_id is not None:
            next_provider_id = str(provider_id or "").strip()

        next_provider_name = llm_config.provider_name
        if provider_name is not None:
            next_provider_name = str(provider_name or "").strip()

        next_api_type = llm_config.api_type
        if api_type is not None:
            next_api_type = str(api_type or "").strip().lower()

        if provider_list:
            resolved = self.resolve_provider(
                provider_list,
                provider_id=next_provider_id,
                provider_name=next_provider_name,
            )
            if resolved is not None:
                next_provider_id = str(getattr(resolved, "id", "") or next_provider_id).strip()
                next_provider_name = str(getattr(resolved, "name", "") or next_provider_name).strip()
                next_api_type = str(getattr(resolved, "api_type", "") or next_api_type).strip().lower()

        next_model = llm_config.model
        if model is not None:
            next_model = str(model or "").strip()

        conversation.set_llm_config(
            llm_config.with_updates(
                provider_id=next_provider_id,
                provider_name=next_provider_name,
                api_type=next_api_type,
                model=next_model,
            )
        )
        return conversation

    def set_title(self, conversation: Conversation, title: str) -> Conversation:
        next_title = str(title or "").strip()
        if next_title:
            conversation.title = next_title
            conversation.updated_at = datetime.now()
        return conversation

    def set_mode(self, conversation: Conversation, mode_slug: str) -> Conversation:
        conversation.mode = str(mode_slug or "chat").strip() or "chat"
        conversation.updated_at = datetime.now()
        return conversation

    def set_work_dir(self, conversation: Conversation, work_dir: str) -> Conversation:
        conversation.work_dir = str(work_dir or "").strip()
        conversation.updated_at = datetime.now()
        return conversation

    def set_setting(self, conversation: Conversation, key: str, value: Any) -> Conversation:
        settings = dict(conversation.settings or {})
        if value is None:
            settings.pop(str(key), None)
        else:
            settings[str(key)] = value
        conversation.settings = settings
        conversation.updated_at = datetime.now()
        return conversation

    def set_settings(self, conversation: Conversation, updates: Dict[str, Any]) -> Conversation:
        settings = dict(conversation.settings or {})
        for key, value in dict(updates or {}).items():
            if value is None:
                settings.pop(str(key), None)
            else:
                settings[str(key)] = value
        conversation.settings = settings
        conversation.updated_at = datetime.now()
        return conversation

    def configure_runtime(
        self,
        conversation: Conversation,
        *,
        mode: str | None = None,
        work_dir: str | None = None,
        show_thinking: bool | None = None,
        enable_mcp: bool | None = None,
        enable_search: bool | None = None,
    ) -> Conversation:
        if mode is not None:
            self.set_mode(conversation, mode)
        if work_dir is not None:
            self.set_work_dir(conversation, work_dir)

        runtime_settings: dict[str, Any] = {}
        if show_thinking is not None:
            runtime_settings["show_thinking"] = bool(show_thinking)
        if enable_mcp is not None:
            runtime_settings["enable_mcp"] = bool(enable_mcp)
        if enable_search is not None:
            runtime_settings["enable_search"] = bool(enable_search)
        if runtime_settings:
            self.set_settings(conversation, runtime_settings)
        return conversation

    def apply_settings_update(
        self,
        conversation: Conversation,
        update: ConversationSettingsUpdate,
        *,
        providers: list | None = None,
    ) -> Conversation:
        provider_list = list(providers or [])
        self.set_title(conversation, update.title)
        self.configure_llm(
            conversation,
            providers=provider_list,
            provider_id=update.provider_id,
            provider_name=update.provider_name,
            api_type=update.api_type,
            model=update.model,
        )

        llm_config = conversation.get_llm_config().with_updates(
            temperature=update.temperature,
            top_p=update.top_p,
            max_tokens=update.max_tokens,
            stream=update.stream,
        )
        conversation.set_llm_config(llm_config)

        self.set_mode(conversation, update.mode_slug)
        self.set_settings(
            conversation,
            {
                "system_prompt": str(update.system_prompt or "").strip() or None,
                "max_context_messages": int(update.max_context_messages)
                if isinstance(update.max_context_messages, int) and int(update.max_context_messages) > 0
                else None,
                "show_thinking": bool(update.show_thinking),
                "enable_mcp": bool(update.enable_mcp),
                "enable_search": bool(update.enable_search),
                "memory_sources": [str(item).strip() for item in (update.memory_sources or ()) if str(item).strip()],
                "allowed_channel_sources": [
                    str(item).strip() for item in (update.allowed_channel_sources or ()) if str(item).strip()
                ],
                "trusted_channel_sources": [
                    str(item).strip() for item in (update.trusted_channel_sources or ()) if str(item).strip()
                ],
                "channel_notice_policy": str(update.channel_notice_policy or "notice").strip().lower() or "notice",
                "summary_model": None,
                "summary_include_tool_details": None,
                "summary_system_prompt": None,
                "prompt_optimizer_model": None,
                "prompt_optimizer_system_prompt": None,
            },
        )
        return conversation

    def build_model_ref(
        self,
        conversation: Conversation,
        providers: list | None = None,
        *,
        provider_id: str | None = None,
        provider_name: str | None = None,
        model: str | None = None,
    ) -> str:
        llm_config = conversation.get_llm_config()
        next_provider_id = str(provider_id if provider_id is not None else (llm_config.provider_id or conversation.provider_id or "")).strip()
        next_provider_name = str(provider_name if provider_name is not None else (llm_config.provider_name or conversation.provider_name or "")).strip()
        next_model = str(model if model is not None else (llm_config.model or conversation.model or "")).strip()

        provider_list = list(providers or [])
        resolved = None
        if provider_list:
            resolved = self.resolve_provider(
                provider_list,
                provider_id=next_provider_id,
                provider_name=next_provider_name,
            )
            if resolved is not None:
                next_provider_name = str(getattr(resolved, "name", "") or next_provider_name).strip()
                if not next_model:
                    next_model = str(getattr(resolved, "default_model", "") or "").strip()

        return build_model_ref(next_provider_name, next_model or llm_config.resolved_model(resolved))
