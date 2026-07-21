from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Iterable

from models.model_ref import provider_matches_name
from models.provider import Provider, normalize_provider_name
from core.app.services.provider import ProviderService
from core.app.repositories.provider import ProviderRepository


logger = logging.getLogger(__name__)


class ProviderCatalogService:
    """Owns provider catalog loading, persistence, and list mutations.

    This separates provider catalog lifecycle from both the network-focused
    ProviderService and the UI layers that edit provider lists.
    """

    def __init__(
        self,
        *,
        repository: ProviderRepository,
        provider_service: ProviderService | None = None,
        model_reference_provider: Callable[[], Iterable[Mapping[str, str]]] | None = None,
    ) -> None:
        self._repository = repository
        self._provider_service = provider_service or ProviderService()
        self._model_reference_provider = model_reference_provider or (lambda: ())
        self._current: list[Provider] = []
        self._loaded = False

    def snapshot(self, providers: Iterable[Provider]) -> list[Provider]:
        return [self.clone_provider(provider) for provider in providers]

    def clone_provider(self, provider: Provider | None = None) -> Provider:
        if provider is None:
            return Provider()
        return Provider.from_dict(provider.to_dict())

    def load(self) -> list[Provider]:
        if self._loaded:
            return self.snapshot(self._current)
        providers = self.snapshot(self._repository.load() or [])
        if not providers:
            providers = self.snapshot(self._provider_service.create_default_providers())
            self._repository.save(providers)
        elif self._repository.needs_migration:
            self._ensure_referenced_models(providers)
            self._repository.save(providers)
        self._current = self.snapshot(providers)
        self._loaded = True
        return self.snapshot(self._current)

    def _ensure_referenced_models(self, providers: list[Provider]) -> None:
        try:
            references = list(self._model_reference_provider() or ())
        except Exception as exc:
            logger.warning("Failed to collect model references for provider migration: %s", exc)
            references = []

        for reference in references:
            if not isinstance(reference, Mapping):
                continue
            provider_id = str(reference.get("provider_id") or "").strip()
            provider_name = str(reference.get("provider_name") or "").strip()
            model_id = str(reference.get("model") or "").strip()
            if not model_id:
                continue
            provider = next(
                (
                    item
                    for item in providers
                    if (provider_id and str(item.id or "").strip() == provider_id)
                    or (provider_name and provider_matches_name(item, provider_name))
                ),
                None,
            )
            if provider is None or provider.find_model_profile(model_id) is not None:
                continue
            provider.upsert_model(provider.effective_model_profile(model_id))

    def save(self, providers: Iterable[Provider]) -> bool:
        snapshot = self.snapshot(providers)
        saved = self._repository.save(snapshot)
        if saved:
            self._current = self.snapshot(snapshot)
            self._loaded = True
        return saved

    def current(self) -> list[Provider]:
        return self.load()

    def find(self, providers: Iterable[Provider], provider_id: str) -> tuple[Provider | None, int]:
        normalized_id = str(provider_id or "").strip()
        provider_list = list(providers)
        for index, provider in enumerate(provider_list):
            if str(getattr(provider, "id", "") or "").strip() == normalized_id:
                return provider, index
        return None, -1

    def select_or_first(self, providers: Iterable[Provider], provider_id: str = "") -> tuple[Provider | None, int]:
        provider_list = list(providers)
        provider, index = self.find(provider_list, provider_id)
        if provider is not None:
            return provider, index
        if provider_list:
            return provider_list[0], 0
        return None, -1

    def upsert(self, providers: Iterable[Provider], provider: Provider) -> list[Provider]:
        next_providers = self.snapshot(providers)
        updated = self.clone_provider(provider)
        for index, existing in enumerate(next_providers):
            if getattr(existing, "id", None) == getattr(updated, "id", None):
                next_providers[index] = updated
                return next_providers
        next_providers.append(updated)
        return next_providers

    def remove(self, providers: Iterable[Provider], provider_id: str) -> list[Provider]:
        normalized_id = str(provider_id or "").strip()
        return [
            self.clone_provider(provider)
            for provider in providers
            if str(getattr(provider, "id", "") or "").strip() != normalized_id
        ]

    def move(self, providers: Iterable[Provider], provider_id: str, delta: int) -> list[Provider]:
        next_providers = self.snapshot(providers)
        _, index = self.find(next_providers, provider_id)
        if index < 0:
            return next_providers
        target = index + int(delta)
        if target < 0 or target >= len(next_providers):
            return next_providers
        next_providers[index], next_providers[target] = next_providers[target], next_providers[index]
        return next_providers

    def set_enabled(self, providers: Iterable[Provider], provider_id: str, enabled: bool) -> list[Provider]:
        next_providers = self.snapshot(providers)
        provider, _index = self.find(next_providers, provider_id)
        if provider is not None:
            provider.enabled = bool(enabled)
        return next_providers

    def toggle_enabled(self, providers: Iterable[Provider], provider_id: str) -> list[Provider]:
        next_providers = self.snapshot(providers)
        provider, _index = self.find(next_providers, provider_id)
        if provider is not None:
            provider.enabled = not bool(getattr(provider, "enabled", True))
        return next_providers

    def merge_defaults(self, providers: Iterable[Provider]) -> tuple[list[Provider], bool]:
        next_providers = self.snapshot(providers)
        existing_names = {
            normalize_provider_name(getattr(provider, "name", "") or "")
            for provider in next_providers
        }
        added_any = False
        for provider in self._provider_service.create_default_providers():
            normalized_name = normalize_provider_name(getattr(provider, "name", "") or "")
            if normalized_name in existing_names:
                continue
            next_providers.append(self.clone_provider(provider))
            existing_names.add(normalized_name)
            added_any = True
        return next_providers, added_any
