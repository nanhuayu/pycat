from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable, Optional

from pycat.core.app.state import EMPTY_APP_STATE, AppState, ConversationSelection, ConversationSettingsUpdate
from pycat.core.app.store import Store
from pycat.models.contracts.config import AppConfig
from pycat.models.contracts.tooling import (
    normalize_filesystem_scope_mode,
    normalize_tool_approval,
)
from pycat.models.conversation import Conversation
from pycat.models.model_ref import build_model_ref, provider_matches_name
from pycat.models.provider import Provider
from pycat.models.session_paths import normalize_work_dir


class AppCoordinator:
    """Application-level conversation orchestration.

    UI gathers input, while this class owns projection into conversation state
    plus a small app store for the active view.
    """

    def __init__(
        self,
        *,
        conv_service: Any,
        store: Store[AppState] | None = None,
        active_processes: Any = None,
    ) -> None:
        self._conv_service = conv_service
        self._store = store or Store(EMPTY_APP_STATE)
        self._active_processes = active_processes

    @property
    def store(self) -> Store[AppState]:
        return self._store

    def invalidate_content(self, work_dir: str, conversation_id: str, domains: tuple[str, ...]) -> None:
        self._store.set_state(lambda previous: replace(previous, content_revision=previous.content_revision + 1,
                              content_workspace=str(work_dir or ""), content_conversation_id=conversation_id,
                              changed_domains=tuple(domains)))

    def sync_catalog(self, *, providers: Iterable[Provider], conversation_count: int | None = None) -> AppState:
        provider_ids = tuple(
            str(getattr(provider, "id", "") or "").strip()
            for provider in providers
            if str(getattr(provider, "id", "") or "").strip()
        )

        def _update(prev: AppState) -> AppState:
            next_state = replace(prev, available_provider_ids=provider_ids)
            if conversation_count is not None:
                next_state = replace(next_state, conversation_count=int(conversation_count))
            return next_state

        self._store.set_state(_update)
        return self._store.get_state()

    def create_conversation(self, selection: ConversationSelection | None = None) -> Conversation:
        conversation = self._conv_service.create()
        if selection is not None:
            self.apply_selection(conversation, selection, initialize_workspace=True)
        return conversation

    def ensure_conversation(
        self,
        conversation: Conversation | None,
        *,
        selection: ConversationSelection,
    ) -> Conversation:
        if conversation is None:
            return self.create_conversation(selection)
        return self.apply_selection(conversation, selection)

    def apply_selection(
        self,
        conversation: Conversation,
        selection: ConversationSelection,
        *,
        initialize_workspace: bool = False,
    ) -> Conversation:
        if initialize_workspace and not normalize_work_dir(
            getattr(conversation, "work_dir", "")
        ):
            conversation.work_dir = normalize_work_dir(selection.work_dir)
        else:
            result = self._conv_service.change_work_dir(
                conversation,
                selection.work_dir,
                active_processes=self._active_processes,
            )
            if not result.ok:
                raise ValueError(result.error)
        self._conv_service.configure_llm(
            conversation,
            provider_id=selection.provider_id,
            provider_name=selection.provider_name,
            api_type=selection.api_type,
            model=selection.model,
        )
        self._conv_service.set_mode(conversation, selection.mode_slug)
        settings: dict[str, Any] = {"show_thinking": bool(selection.show_thinking)}
        if selection.tool_approval is not None:
            settings["tool_approval"] = normalize_tool_approval(selection.tool_approval)
        if selection.filesystem_mode is not None:
            settings["filesystem_mode"] = normalize_filesystem_scope_mode(
                selection.filesystem_mode
            )
        self._conv_service.set_settings(conversation, settings)
        return conversation

    def update_provider_model(
        self,
        conversation: Conversation,
        *,
        providers: Iterable[Provider],
        provider_id: str,
        model: str,
    ) -> Conversation:
        next_provider_id = str(provider_id or conversation.provider_id or "").strip()
        provider_name = str(getattr(conversation, "provider_name", "") or "").strip()
        if next_provider_id:
            provider = self._conv_service.find_provider(list(providers), next_provider_id)
            if provider is not None:
                provider_name = str(getattr(provider, "name", "") or "").strip()
        self._conv_service.configure_llm(
            conversation,
            providers=list(providers),
            provider_id=next_provider_id,
            provider_name=provider_name,
            model=str(model or conversation.model or "").strip(),
        )
        return conversation

    def apply_mode(self, conversation: Conversation, mode_slug: str) -> Conversation:
        self._conv_service.set_mode(conversation, mode_slug)
        return conversation

    def apply_work_dir(self, conversation: Conversation, work_dir: str, *, active_processes: Any = None) -> Any:
        return self._conv_service.change_work_dir(
            conversation,
            work_dir,
            active_processes=self._active_processes if active_processes is None else active_processes,
        )

    def apply_settings_update(
        self,
        conversation: Conversation,
        *,
        update: ConversationSettingsUpdate,
        providers: Iterable[Provider],
    ) -> Conversation:
        self._conv_service.apply_settings_update(
            conversation,
            update,
            providers=list(providers),
        )
        return conversation

    def build_app_state(
        self,
        conversation: Conversation | None,
        *,
        providers: Iterable[Provider],
        app_settings: Optional[dict[str, Any]] = None,
        is_streaming: bool = False,
    ) -> AppState:
        enabled_channel_sources = self._resolve_enabled_channel_sources(app_settings)
        if conversation is None:
            return replace(
                EMPTY_APP_STATE,
                is_streaming=bool(is_streaming),
                memory_enabled=True,
                enabled_channel_sources=enabled_channel_sources,
                allowed_channel_sources=enabled_channel_sources,
                trusted_channel_sources=tuple(),
                channel_notice_policy="notice",
            )

        llm_config = conversation.get_llm_config()
        provider_list = list(providers)
        resolved_provider = self._conv_service.resolve_provider(
            provider_list,
            provider_id=llm_config.provider_id or conversation.provider_id,
            provider_name=llm_config.provider_name or getattr(conversation, "provider_name", ""),
        )
        provider_name = (
            str(getattr(resolved_provider, "name", "") or "").strip()
            if resolved_provider is not None
            else self.resolve_provider_name(
                provider_list,
                provider_id=llm_config.provider_id or conversation.provider_id,
                provider_name=llm_config.provider_name or getattr(conversation, "provider_name", ""),
            )
        )
        settings = getattr(conversation, "settings", {}) or {}
        defaults = app_settings or {}
        model = llm_config.resolved_model() or getattr(conversation, "model", "") or ""
        api_type = llm_config.resolved_api_type(resolved_provider)
        memory_enabled = bool((getattr(conversation, "settings", {}) or {}).get("memory_enabled", True))
        allowed_channel_sources = self._resolve_allowed_channel_sources(
            conversation,
            enabled_sources=enabled_channel_sources,
        )
        trusted_channel_sources = self._resolve_trusted_channel_sources(
            conversation,
            allowed_sources=allowed_channel_sources,
        )
        channel_notice_policy = self._resolve_channel_notice_policy(conversation)

        return AppState(
            current_conversation_id=str(getattr(conversation, "id", "") or ""),
            provider_id=str(llm_config.provider_id or getattr(conversation, "provider_id", "") or ""),
            provider_name=provider_name,
            api_type=api_type,
            model=str(model or "").strip(),
            model_ref=build_model_ref(provider_name, str(model or "").strip()),
            mode_slug=str(getattr(conversation, "mode", "chat") or "chat").strip() or "chat",
            work_dir=str(getattr(conversation, "work_dir", "") or "").strip(),
            show_thinking=bool(settings.get("show_thinking", defaults.get("show_thinking", True))),
            message_count=len(getattr(conversation, "messages", []) or []),
            is_streaming=bool(is_streaming),
            memory_enabled=memory_enabled,
            enabled_channel_sources=enabled_channel_sources,
            allowed_channel_sources=allowed_channel_sources,
            trusted_channel_sources=trusted_channel_sources,
            channel_notice_policy=channel_notice_policy,
        )

    def remember_current_conversation(
        self,
        conversation: Conversation | None,
        *,
        providers: Iterable[Provider],
        app_settings: Optional[dict[str, Any]] = None,
        is_streaming: bool = False,
    ) -> AppState:
        next_state = self.build_app_state(
            conversation,
            providers=providers,
            app_settings=app_settings,
            is_streaming=is_streaming,
        )
        def _update(prev: AppState) -> AppState:
            return replace(
                next_state,
                available_provider_ids=prev.available_provider_ids,
                conversation_count=prev.conversation_count,
            )

        self._store.set_state(_update)
        return self._store.get_state()

    def set_streaming(self, conversation_id: str, *, is_streaming: bool) -> AppState:
        conversation_key = str(conversation_id or "")

        def _update(prev: AppState) -> AppState:
            if prev.current_conversation_id != conversation_key:
                return prev
            return replace(prev, is_streaming=bool(is_streaming))

        self._store.set_state(_update)
        return self._store.get_state()

    def clear_current_conversation(self) -> AppState:
        def _update(prev: AppState) -> AppState:
            return replace(
                EMPTY_APP_STATE,
                enabled_channel_sources=prev.enabled_channel_sources,
                available_provider_ids=prev.available_provider_ids,
                conversation_count=prev.conversation_count,
            )

        self._store.set_state(_update)
        return self._store.get_state()

    def resolve_provider_name(
        self,
        providers: Iterable[Provider],
        *,
        provider_id: str = "",
        provider_name: str = "",
    ) -> str:
        normalized_id = str(provider_id or "").strip()
        normalized_name = str(provider_name or "").strip()
        provider_list = list(providers)
        if normalized_id:
            provider = self._conv_service.find_provider(provider_list, normalized_id)
            if provider is not None:
                return str(getattr(provider, "name", "") or "").strip()
        if normalized_name:
            for provider in provider_list:
                if provider_matches_name(provider, normalized_name):
                    return str(getattr(provider, "name", "") or "").strip()
        return normalized_name

    def build_model_ref(self, *, providers: Iterable[Provider], provider_id: str, provider_name: str = "", model: str = "") -> str:
        provider_list = list(providers)
        resolved_name = self.resolve_provider_name(
            provider_list,
            provider_id=provider_id,
            provider_name=provider_name,
        )
        resolved_model = str(model or "").strip()
        return build_model_ref(resolved_name, resolved_model)

    @staticmethod
    def _resolve_enabled_channel_sources(app_settings: Optional[dict[str, Any]]) -> tuple[str, ...]:
        config = AppConfig.from_dict(app_settings or {})
        seen: set[str] = set()
        sources: list[str] = []
        for channel in getattr(config, "channels", []) or []:
            if not bool(getattr(channel, "enabled", False)):
                continue
            source = str(getattr(channel, "source", "") or "").strip()
            if not source or source in seen:
                continue
            seen.add(source)
            sources.append(source)
        return tuple(sources)

    @staticmethod
    def _normalize_string_tuple(
        values: Any,
        *,
        allowed: Iterable[str] | None = None,
    ) -> tuple[str, ...]:
        if isinstance(values, str):
            candidates = [part.strip() for part in values.split(",")]
        elif isinstance(values, (list, tuple, set)):
            candidates = [str(item).strip() for item in values]
        else:
            candidates = []

        allowed_set = {str(item).strip() for item in (allowed or ()) if str(item).strip()}
        normalized: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            if not item or item in seen:
                continue
            if allowed_set and item not in allowed_set:
                continue
            seen.add(item)
            normalized.append(item)
        return tuple(normalized)

    def _resolve_allowed_channel_sources(
        self,
        conversation: Conversation,
        *,
        enabled_sources: tuple[str, ...],
    ) -> tuple[str, ...]:
        settings = getattr(conversation, "settings", {}) or {}
        normalized = self._normalize_string_tuple(
            settings.get("allowed_channel_sources"),
            allowed=enabled_sources,
        )
        return normalized or tuple(enabled_sources)

    def _resolve_trusted_channel_sources(
        self,
        conversation: Conversation,
        *,
        allowed_sources: tuple[str, ...],
    ) -> tuple[str, ...]:
        settings = getattr(conversation, "settings", {}) or {}
        return self._normalize_string_tuple(
            settings.get("trusted_channel_sources"),
            allowed=allowed_sources,
        )

    @staticmethod
    def _resolve_channel_notice_policy(conversation: Conversation) -> str:
        settings = getattr(conversation, "settings", {}) or {}
        policy = str(settings.get("channel_notice_policy", "notice") or "notice").strip().lower() or "notice"
        if policy not in {"notice", "strict", "silent"}:
            return "notice"
        return policy
