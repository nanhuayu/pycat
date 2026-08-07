"""Application use case for cross-domain settings updates.

The service owns the complete control-plane sequence:

    validate -> persist domains -> reconcile runtime -> reconcile channels

Persistence is intentionally not transactional across files. Each domain
reports saved/error independently, already-saved domains are retained, and
the returned snapshot always describes the state adapters should show.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from core.app.state import AppSettingsUpdate
from models.contracts.config import AppConfig
from models.contracts.mcp import McpServerConfig
from models.contracts.mode import ModeConfig
from models.provider import Provider
from models.search_config import SearchConfig


logger = logging.getLogger(__name__)

DOMAIN_PROVIDERS = "providers"
DOMAIN_APP_SETTINGS = "app_settings"
DOMAIN_MCP = "mcp"
DOMAIN_MODES = "modes"
DOMAIN_SEARCH = "search"

STAGE_VALIDATE = "validate"
STAGE_RUNTIME = "runtime"
STAGE_CHANNEL = "channel"


@dataclass(frozen=True)
class SettingsSnapshot:
    """Canonical settings state exposed to application adapters."""

    app_settings: dict[str, Any]
    providers: tuple[Provider, ...] = ()
    mcp_servers: tuple[McpServerConfig, ...] = ()
    modes: tuple[ModeConfig, ...] = ()
    search_config: SearchConfig | None = None


@dataclass(frozen=True)
class SettingsApplyResult:
    """Outcome of the complete settings control-plane operation."""

    snapshot: SettingsSnapshot
    saved_domains: tuple[str, ...] = ()
    domain_errors: tuple[tuple[str, str], ...] = ()
    stage_errors: tuple[tuple[str, str], ...] = ()
    runtime_applied: bool = False
    channel_restarted: bool = False

    @property
    def app_settings(self) -> dict[str, Any]:
        return self.snapshot.app_settings

    @property
    def failed_domains(self) -> tuple[str, ...]:
        return tuple(domain for domain, _error in self.domain_errors)

    @property
    def failed_stages(self) -> tuple[tuple[str, str], ...]:
        return self.stage_errors

    @property
    def ok(self) -> bool:
        return not self.domain_errors and not self.stage_errors


class SettingsUpdateService:
    """Load, persist, and reconcile the application's settings domains."""

    def __init__(
        self,
        *,
        app_settings_service: Any,
        provider_catalog_service: Any,
        mode_catalog_service: Any,
        repositories: Any,
        channel_gateway: Any,
        channel_service: Any,
        runtime_config_applier: Callable[[dict[str, Any]], AppConfig],
    ) -> None:
        self._app_settings_service = app_settings_service
        self._provider_catalog_service = provider_catalog_service
        self._mode_catalog_service = mode_catalog_service
        self._repositories = repositories
        self._channel_gateway = channel_gateway
        self._channel_service = channel_service
        self._runtime_config_applier = runtime_config_applier

    def load_snapshot(self) -> SettingsSnapshot:
        """Load all settings domains through their application-owned ports."""

        return SettingsSnapshot(
            app_settings=dict(
                self._load(self._app_settings_service.load, {}, DOMAIN_APP_SETTINGS) or {}
            ),
            providers=tuple(
                self._load(self._provider_catalog_service.current, (), DOMAIN_PROVIDERS) or ()
            ),
            mcp_servers=tuple(
                self._load(self._repositories.mcp_servers.load, (), DOMAIN_MCP) or ()
            ),
            modes=tuple(self._load(self._mode_catalog_service.load, (), DOMAIN_MODES) or ()),
            search_config=self._load(
                self._repositories.search_config.load, SearchConfig(), DOMAIN_SEARCH
            ),
        )

    def apply(
        self,
        update: AppSettingsUpdate,
        *,
        current_settings: dict[str, Any],
        current_providers: Iterable[Provider] = (),
    ) -> SettingsApplyResult:
        # "Before" state: the caller's in-memory values for the two domains the
        # host already owns (they may carry unsaved UI keys), repository state
        # for the domains only the dialog has seen.
        before = SettingsSnapshot(
            app_settings=dict(current_settings or {}),
            providers=tuple(current_providers or ()),
            mcp_servers=tuple(self._load(self._repositories.mcp_servers.load, (), DOMAIN_MCP) or ()),
            modes=tuple(self._load(self._mode_catalog_service.load, (), DOMAIN_MODES) or ()),
            search_config=self._load(
                self._repositories.search_config.load, SearchConfig(), DOMAIN_SEARCH
            ),
        )
        try:
            next_settings = self._app_settings_service.apply_update(current_settings, update)
        except Exception as exc:
            logger.warning("Failed to validate settings update: %s", exc)
            return SettingsApplyResult(
                snapshot=before,
                stage_errors=((STAGE_VALIDATE, str(exc or "unknown error")),),
            )

        saves: list[tuple[str, Callable[[], bool]]] = [
            (DOMAIN_PROVIDERS, lambda: self._provider_catalog_service.save(list(update.providers))),
            (DOMAIN_APP_SETTINGS, lambda: self._app_settings_service.save(next_settings)),
            (DOMAIN_MCP, lambda: self._repositories.mcp_servers.save(list(update.mcp_servers))),
            (DOMAIN_MODES, lambda: self._mode_catalog_service.save(list(update.modes))),
        ]
        if update.search_config is not None:
            saves.append((DOMAIN_SEARCH, lambda: self._repositories.search_config.save(update.search_config)))

        saved: list[str] = []
        domain_errors: list[tuple[str, str]] = []
        for domain, save in saves:
            try:
                persisted = bool(save())
                error = "" if persisted else "save returned false"
            except Exception as exc:
                persisted, error = False, str(exc or "unknown error")
            if persisted:
                saved.append(domain)
            else:
                logger.warning("Failed to persist settings domain %s: %s", domain, error)
                domain_errors.append((domain, error))

        saved_set = set(saved)
        snapshot = SettingsSnapshot(
            app_settings=dict(next_settings if DOMAIN_APP_SETTINGS in saved_set else before.app_settings),
            providers=tuple(update.providers if DOMAIN_PROVIDERS in saved_set else before.providers),
            mcp_servers=tuple(update.mcp_servers if DOMAIN_MCP in saved_set else before.mcp_servers),
            modes=tuple(update.modes if DOMAIN_MODES in saved_set else before.modes),
            search_config=(
                update.search_config
                if DOMAIN_SEARCH in saved_set and update.search_config is not None
                else before.search_config
            ),
        )

        stage_errors: list[tuple[str, str]] = []
        runtime_applied = False
        try:
            self._runtime_config_applier(snapshot.app_settings)
            runtime_applied = True
        except Exception as exc:
            logger.warning("Failed to apply persisted runtime configuration: %s", exc)
            stage_errors.append((STAGE_RUNTIME, str(exc or "unknown error")))

        channel_restarted = False
        try:
            desired = tuple(self._channel_service.runtime_channels(snapshot.app_settings))
            current = tuple(self._channel_gateway.configured_channels())
        except Exception as exc:
            logger.warning("Failed to resolve channel configuration: %s", exc)
            stage_errors.append((STAGE_CHANNEL, str(exc or "unknown error")))
        else:
            if self._channels_snapshot(current) != self._channels_snapshot(desired):
                try:
                    self._channel_gateway.start(desired)
                    channel_restarted = True
                except Exception as exc:
                    logger.warning("Failed to reconcile channel gateway: %s", exc)
                    stage_errors.append((STAGE_CHANNEL, str(exc or "unknown error")))

        return SettingsApplyResult(
            snapshot=snapshot,
            saved_domains=tuple(saved),
            domain_errors=tuple(domain_errors),
            stage_errors=tuple(stage_errors),
            runtime_applied=runtime_applied,
            channel_restarted=channel_restarted,
        )

    @staticmethod
    def _channels_snapshot(channels: Iterable[Any]) -> tuple[str, ...]:
        values = []
        for channel in channels:
            payload = channel.to_dict() if hasattr(channel, "to_dict") else channel
            values.append(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
        return tuple(sorted(values))

    @staticmethod
    def _load(loader: Callable[[], Any], default: Any, domain: str) -> Any:
        try:
            return loader()
        except Exception as exc:
            logger.warning("Failed to load settings domain %s: %s", domain, exc)
            return default


__all__ = [
    "SettingsApplyResult",
    "SettingsSnapshot",
    "SettingsUpdateService",
]
