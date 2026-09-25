"""Application use case for cross-domain settings updates.

The service owns the complete control-plane sequence:

    validate -> persist domains -> reconcile runtime -> reconcile channels

Persistence is intentionally not transactional across files. Each domain
reports saved/error independently, already-saved domains are retained, and
the returned snapshot always describes the state adapters should show.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from pycat.core.app.serialization import json_value, merge_draft, redact
from pycat.core.app.state import AppSettingsUpdate
from pycat.models.contracts.agent import InvalidRequestError
from pycat.models.contracts.config import AppConfig
from pycat.models.contracts.mcp import McpServerConfig
from pycat.models.contracts.mode import ModeConfig
from pycat.models.contracts.model_target import ModelTarget
from pycat.models.provider import Provider
from pycat.models.search_config import SearchConfig

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
        self._lock = threading.RLock()

    def _values(self):
        values = json_value(self.load_snapshot())
        values['app_settings'] = {**values['app_settings'], **AppConfig.from_dict(values['app_settings']).to_dict()}
        return values

    @staticmethod
    def _revision(values):
        return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def view(self) -> dict:
        with self._lock:
            values = self._values()
            return {'revision': self._revision(values), 'values': redact(values)}

    def update(self, patch: dict, *, expected_revision: str) -> dict:
        with self._lock:
            current = self._values()
            if not expected_revision or expected_revision != self._revision(current):
                raise InvalidRequestError('Configuration changed; reload before saving.')
            if set(patch) - set(current):
                raise InvalidRequestError('Unknown configuration domain.')
            values = merge_draft(current, patch)
            providers = tuple(Provider.from_dict(item) for item in values['providers'])
            if 'providers' in patch:
                names = [item.canonical_name for item in providers]
                if any(not name for name in names):
                    raise InvalidRequestError('Provider name must contain ASCII letters or digits.')
                if len(names) != len(set(names)):
                    raise InvalidRequestError('Provider names must be unique.')
            try:
                modes = tuple(ModeConfig(**{**item, 'model_target': ModelTarget.from_dict(item.get('model_target', {}))})
                              for item in values['modes'])
            except (TypeError, ValueError) as exc:
                raise InvalidRequestError(f'Invalid mode configuration: {exc}') from exc
            if 'channels' in patch.get('app_settings', {}):
                prepared = self._channel_service.prepare_for_save(values['app_settings']['channels'])
                values['app_settings']['channels'] = json_value(prepared.channels)
            result = self.apply(AppSettingsUpdate(
                providers=providers,
                settings_patch=values['app_settings'],
                mcp_servers=tuple(McpServerConfig.from_dict(item) for item in values['mcp_servers']),
                modes=modes, search_config=SearchConfig.from_dict(values['search_config'] or {})),
                current_settings=current['app_settings'], current_providers=self._provider_catalog_service.current(),
                domains={ {'providers': DOMAIN_PROVIDERS, 'app_settings': DOMAIN_APP_SETTINGS, 'mcp_servers': DOMAIN_MCP,
                           'modes': DOMAIN_MODES, 'search_config': DOMAIN_SEARCH}[key] for key in patch })
            return {'ok': result.ok, 'saved_domains': list(result.saved_domains),
                    'domain_errors': list(result.domain_errors), 'stage_errors': list(result.stage_errors), **self.view()}

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

    def load(self) -> dict[str, Any]:
        return self._app_settings_service.load()

    def save(self, settings_patch: dict[str, Any]) -> SettingsApplyResult:
        """Apply a partial settings update and refresh the existing runtime."""
        current = self.load_snapshot()
        return self.apply(AppSettingsUpdate(providers=current.providers, settings_patch=dict(settings_patch),
            mcp_servers=current.mcp_servers, modes=current.modes, search_config=current.search_config),
            current_settings=current.app_settings, current_providers=current.providers)

    def save_mcp(self, servers) -> SettingsApplyResult:
        current = self.load_snapshot()
        return self.apply(AppSettingsUpdate(providers=current.providers, mcp_servers=tuple(servers),
            modes=current.modes, search_config=current.search_config),
            current_settings=current.app_settings, current_providers=current.providers)

    def apply(self, update: AppSettingsUpdate, *, current_settings: dict[str, Any], current_providers=(), domains=None) -> SettingsApplyResult:
        with self._lock:
            return self._apply(update, current_settings=current_settings, current_providers=current_providers, domains=domains)

    def _apply(
        self,
        update: AppSettingsUpdate,
        *,
        current_settings: dict[str, Any],
        current_providers: Iterable[Provider] = (),
        domains=None,
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
            if domains is not None and domain not in domains:
                continue
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
