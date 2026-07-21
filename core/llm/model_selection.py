from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from models.contracts.model_target import ModelTarget
from models.provider import Provider, provider_matches_name, split_model_ref


@dataclass(frozen=True)
class ResolvedModelSelection:
    provider: Provider | None = None
    model: str = ""
    source: str = ""
    fallback_from: str = ""

    @property
    def provider_id(self) -> str:
        return str(getattr(self.provider, "id", "") or "").strip() if self.provider is not None else ""

    @property
    def provider_name(self) -> str:
        return str(getattr(self.provider, "name", "") or "").strip() if self.provider is not None else ""

    @property
    def api_type(self) -> str:
        return str(getattr(self.provider, "api_type", "") or "").strip().lower() if self.provider is not None else ""


def provider_model_ids(provider: Provider) -> tuple[str, ...]:
    """Return ids from the provider's curated model catalog."""

    return tuple(provider.model_ids())


def provider_has_model(provider: Provider, model: str) -> bool:
    target = str(model or "").strip()
    return bool(target and target in provider_model_ids(provider))


def model_ref_is_available(providers: Iterable[Provider], model_ref: str) -> bool:
    """Return whether a model reference points to an enabled, configured model."""

    enabled = [provider for provider in providers or [] if bool(getattr(provider, "enabled", True))]
    resolved = resolve_provider_model_ref(enabled, model_ref)
    return bool(
        resolved.provider is not None
        and resolved.model
        and provider_has_model(resolved.provider, resolved.model)
    )


def resolve_provider_model_ref(
    providers: Iterable[Provider],
    model_ref: str,
) -> ResolvedModelSelection:
    provider_list = list(providers or [])
    provider_name, model_name = split_model_ref(model_ref)
    model = str(model_name or "").strip()
    if not model:
        return ResolvedModelSelection()

    if provider_name:
        for provider in provider_list:
            if provider_matches_name(provider, provider_name):
                return ResolvedModelSelection(provider=provider, model=model, source="reference")

    matches = [provider for provider in provider_list if provider_has_model(provider, model)]
    if len(matches) == 1:
        return ResolvedModelSelection(provider=matches[0], model=model, source="reference")
    return ResolvedModelSelection(model=model)


def select_default_provider_model(
    providers: Iterable[Provider],
    *,
    default_model_ref: str = "",
) -> ResolvedModelSelection:
    provider_list = [provider for provider in providers or [] if bool(getattr(provider, "enabled", True))]
    if not provider_list:
        return ResolvedModelSelection()

    resolved_default = resolve_provider_model_ref(provider_list, default_model_ref)
    if (
        resolved_default.provider is not None
        and provider_has_model(resolved_default.provider, resolved_default.model)
    ):
        return ResolvedModelSelection(
            provider=resolved_default.provider,
            model=resolved_default.model,
            source="application_default",
        )

    for provider in provider_list:
        models = provider_model_ids(provider)
        if models:
            return ResolvedModelSelection(provider=provider, model=models[0], source="catalog_first")

    return ResolvedModelSelection(provider=provider_list[0], model="", source="provider")


def resolve_model_target(
    providers: Iterable[Provider],
    target: ModelTarget,
    *,
    primary_provider: Provider | None,
    primary_model: str = "",
    auxiliary_model_ref: str = "",
) -> ResolvedModelSelection:
    """Resolve one role-aware model target with deterministic fallbacks."""

    enabled = [provider for provider in providers or [] if bool(getattr(provider, "enabled", True))]

    def resolve_available_ref(model_ref: str) -> ResolvedModelSelection:
        resolved = resolve_provider_model_ref(enabled, model_ref)
        if (
            resolved.provider is not None
            and resolved.model
            and provider_has_model(resolved.provider, resolved.model)
        ):
            return resolved
        return ResolvedModelSelection()

    primary_model_id = str(primary_model or "").strip()
    if not primary_model_id and primary_provider is not None:
        primary_ids = provider_model_ids(primary_provider)
        primary_model_id = primary_ids[0] if primary_ids else ""
    primary = ResolvedModelSelection(provider=primary_provider, model=primary_model_id, source="primary")
    if primary.provider is None or not bool(getattr(primary.provider, "enabled", True)):
        primary = select_default_provider_model(enabled)

    preference = target if isinstance(target, ModelTarget) else ModelTarget()
    if preference.source == "primary":
        return ResolvedModelSelection(provider=primary.provider, model=primary.model, source="primary")

    fallback_from = ""
    if preference.source == "explicit":
        explicit = resolve_available_ref(preference.model_ref)
        if explicit.provider is not None:
            return ResolvedModelSelection(provider=explicit.provider, model=explicit.model, source="explicit")
        fallback_from = "explicit"

    auxiliary = resolve_available_ref(auxiliary_model_ref)
    if auxiliary.provider is not None:
        return ResolvedModelSelection(
            provider=auxiliary.provider,
            model=auxiliary.model,
            source="auxiliary",
            fallback_from=fallback_from,
        )
    if not fallback_from and str(auxiliary_model_ref or "").strip():
        fallback_from = "auxiliary"
    return ResolvedModelSelection(
        provider=primary.provider,
        model=primary.model,
        source="primary",
        fallback_from=fallback_from,
    )
