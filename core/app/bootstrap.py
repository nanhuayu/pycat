from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from models.provider import Provider
from services.app_settings_service import AppSettingsService
from services.conversation_service import ConversationService
from services.provider_catalog_service import ProviderCatalogService


@dataclass(frozen=True)
class AppBootstrapState:
    settings: dict[str, Any]
    providers: tuple[Provider, ...]
    conversations: tuple[dict[str, Any], ...]
    show_stats: bool = True
    splitter_sizes: tuple[int, int, int] | None = None
    chat_splitter_sizes: tuple[int, int] | None = None


class AppBootstrap:
    """Loads initial persisted application state for the UI bootstrap path.

    The UI should orchestrate widgets, while persistence/bootstrap loading
    lives in a dedicated application service.
    """

    def __init__(
        self,
        *,
        app_settings_service: AppSettingsService,
        provider_catalog_service: ProviderCatalogService,
        conv_service: ConversationService,
    ) -> None:
        self._app_settings_service = app_settings_service
        self._provider_catalog_service = provider_catalog_service
        self._conv_service = conv_service

    def load(self) -> AppBootstrapState:
        settings = self._app_settings_service.load()

        providers = self._provider_catalog_service.load()

        conversations = tuple(self._conv_service.list_all())

        return AppBootstrapState(
            settings=settings,
            providers=tuple(providers),
            conversations=conversations,
            show_stats=bool(settings.get("show_stats", True)),
            splitter_sizes=self._coerce_sizes(settings.get("splitter_sizes"), expected=3),
            chat_splitter_sizes=self._coerce_sizes(settings.get("chat_splitter_sizes"), expected=2),
        )

    @staticmethod
    def _coerce_sizes(value: Any, *, expected: int) -> tuple[int, ...] | None:
        if not isinstance(value, list) or len(value) != expected or not all(isinstance(x, int) for x in value):
            return None
        return tuple(int(x) for x in value)