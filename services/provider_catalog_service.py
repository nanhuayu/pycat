from __future__ import annotations

from typing import Iterable

from models.provider import Provider, normalize_provider_name
from services.provider_service import ProviderService
from services.storage_service import StorageService


class ProviderCatalogService:
    """Owns provider catalog loading, persistence, and list mutations.

    This separates provider catalog lifecycle from both the network-focused
    ProviderService and the UI layers that edit provider lists.
    """

    def __init__(
        self,
        *,
        storage: StorageService,
        provider_service: ProviderService | None = None,
    ) -> None:
        self._storage = storage
        self._provider_service = provider_service or ProviderService()

    def snapshot(self, providers: Iterable[Provider]) -> list[Provider]:
        return [self.clone_provider(provider) for provider in providers]

    def clone_provider(self, provider: Provider | None = None) -> Provider:
        if provider is None:
            return Provider()
        return Provider.from_dict(provider.to_dict())

    def load(self) -> list[Provider]:
        providers = self.snapshot(self._storage.load_providers() or [])
        if providers:
            return providers
        defaults = self.snapshot(self._provider_service.create_default_providers())
        self._storage.save_providers(defaults)
        return defaults

    def save(self, providers: Iterable[Provider]) -> bool:
        return self._storage.save_providers(self.snapshot(providers))

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