from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from importlib import resources
from pathlib import Path
from typing import Iterable

from core.app.repositories.provider import ProviderRepository
from core.app.services.provider import ProviderService
from models.model_profile import (
    MODEL_INPUT_MODALITIES,
    REASONING_CODECS,
    ModelProfile,
    reasoning_codecs_for_provider,
)
from models.model_ref import provider_matches_name
from models.provider import Provider

logger = logging.getLogger(__name__)
DEFAULT_MODELS_FILENAME = "default_models.json"


def _parse_catalog_payload(text: str, *, source: str) -> dict:
    try:
        payload = json.loads(text)
    except Exception as exc:
        raise ValueError(f"{source} 不是有效 JSON: {exc}") from exc
    if not isinstance(payload, dict) or int(payload.get("schema_version") or 0) != 1:
        raise ValueError(f"{source} schema_version 无效")
    if not isinstance(payload.get("providers"), list):
        raise ValueError(f"{source} providers 必须是数组")
    return payload


def _bundled_catalog_payload() -> dict:
    resource = resources.files("core.app").joinpath(DEFAULT_MODELS_FILENAME)
    try:
        text = resource.read_text(encoding="utf-8")
    except Exception as exc:
        raise RuntimeError(f"无法读取内置模型目录: {exc}") from exc
    return _parse_catalog_payload(text, source="内置模型目录")


def _validate_default_model(
    raw: Mapping,
    *,
    provider_key: str,
    api_type: str,
    seen: set[str],
) -> None:
    model_id = str(raw.get("model_id") or "").strip()
    if not model_id or model_id in seen:
        raise ValueError(f"默认模型目录 {provider_key} 包含空或重复 model_id: {model_id}")
    seen.add(model_id)
    for field_name, allowed in (("input_modalities", MODEL_INPUT_MODALITIES),):
        values = raw.get(field_name, ["text"])
        if not isinstance(values, list) or any(str(item).strip().lower() not in allowed for item in values):
            raise ValueError(f"默认模型目录 {provider_key}/{model_id} 的 {field_name} 无效")
    codec = str(raw.get("reasoning_codec") or "none").strip().lower()
    if codec not in REASONING_CODECS:
        raise ValueError(f"默认模型目录 {provider_key}/{model_id} 的 reasoning_codec 无效")
    if codec not in reasoning_codecs_for_provider(api_type, provider_key):
        raise ValueError(f"默认模型目录 {provider_key}/{model_id} 的 reasoning_codec 与接口类型不匹配")
    for field_name in ("context_window", "max_output_tokens"):
        value = raw.get(field_name)
        if value not in (None, "") and (not isinstance(value, int) or value <= 0):
            raise ValueError(f"默认模型目录 {provider_key}/{model_id} 的 {field_name} 无效")


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
        default_models_path: str | Path | None = None,
    ) -> None:
        self._repository = repository
        self._provider_service = provider_service or ProviderService()
        self._model_reference_provider = model_reference_provider or (lambda: ())
        self._default_models_path = (
            Path(default_models_path)
            if default_models_path is not None
            else repository.data_dir / DEFAULT_MODELS_FILENAME
        )
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
        if not providers and not self._repository.has_catalog and not self._repository.catalog_file_exists:
            providers = self.load_defaults()
            self._repository.save(providers)
        elif self._repository.needs_migration:
            self._ensure_referenced_models(providers)
            self._repository.save(providers)
        self._current = self.snapshot(providers)
        self._loaded = True
        return self.snapshot(self._current)

    def load_defaults(self) -> list[Provider]:
        if self._default_models_path.is_file():
            try:
                payload = _parse_catalog_payload(
                    self._default_models_path.read_text(encoding="utf-8"),
                    source=str(self._default_models_path),
                )
                return self._providers_from_default_payload(payload)
            except Exception as exc:
                logger.warning(
                    "Ignoring invalid editable model defaults at %s: %s",
                    self._default_models_path,
                    exc,
                )

        payload = _bundled_catalog_payload()
        providers = self._providers_from_default_payload(payload)
        if not self._default_models_path.exists():
            try:
                self._default_models_path.parent.mkdir(parents=True, exist_ok=True)
                self._default_models_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            except Exception as exc:
                logger.warning(
                    "Unable to materialize editable model defaults at %s: %s",
                    self._default_models_path,
                    exc,
                )
        return providers

    def _providers_from_default_payload(self, payload: Mapping) -> list[Provider]:
        providers: list[Provider] = []
        seen_keys: set[str] = set()
        for raw in payload["providers"]:
            if not isinstance(raw, Mapping):
                raise ValueError("默认模型目录 provider 必须是对象")
            provider_key = str(raw.get("catalog_key") or "").strip().lower()
            if not provider_key or provider_key in seen_keys:
                raise ValueError(f"默认模型目录包含空或重复 catalog_key: {provider_key}")
            seen_keys.add(provider_key)
            seen_models: set[str] = set()
            model_values = raw.get("models", [])
            if not isinstance(model_values, list):
                raise ValueError(f"默认模型目录 {provider_key} 的 models 必须是数组")
            for model in model_values:
                if not isinstance(model, Mapping):
                    raise ValueError(f"默认模型目录 {provider_key} 的 model 必须是对象")
                _validate_default_model(
                    model,
                    provider_key=provider_key,
                    api_type=str(raw.get("api_type") or "openai_compatible"),
                    seen=seen_models,
                )
            provider = Provider.from_dict(dict(raw))
            provider.enabled = False
            providers.append(provider)
        return self.snapshot(providers)

    def enrich_discovered_models(
        self,
        provider: Provider,
        profiles: Iterable[ModelProfile],
    ) -> list[ModelProfile]:
        """Apply exact bundled metadata to generic discovery results."""

        defaults = self.load_defaults()
        seed_provider = next(
            (
                item
                for item in defaults
                if provider.catalog_key and item.catalog_key == provider.catalog_key
            ),
            None,
        )
        seeds = {item.model_id: item for item in (seed_provider.models if seed_provider else [])}
        enriched: list[ModelProfile] = []
        for profile in profiles:
            discovered = ModelProfile.from_dict(profile.to_dict())
            seed = seeds.get(discovered.model_id)
            if seed is not None and provider.catalog_key != "openrouter":
                discovered = ModelProfile.from_dict(seed.to_dict())
            enriched.append(discovered)
        return enriched

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

    def save_model_profile(self, provider_id: str, profile: ModelProfile) -> list[Provider]:
        """Persist one model profile through the catalog's single write path."""

        providers = self.current()
        provider, _index = self.find(providers, provider_id)
        if provider is None:
            raise ValueError("服务商不存在")
        provider.upsert_model(profile.as_user_managed())
        if not self.save(providers):
            raise RuntimeError("无法保存模型配置")
        return self.current()

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
