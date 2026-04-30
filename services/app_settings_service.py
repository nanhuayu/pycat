from __future__ import annotations

from typing import Any

from core.app.state import AppSettingsUpdate
from services.storage_service import StorageService


class AppSettingsService:
    """Owns persisted application settings load/save/merge behavior."""

    def __init__(self, storage: StorageService) -> None:
        self._storage = storage

    def load(self) -> dict[str, Any]:
        return dict(self._storage.load_settings() or {})

    def save(self, settings: dict[str, Any]) -> bool:
        return self._storage.save_settings(dict(settings or {}))

    def apply_update(
        self,
        current_settings: dict[str, Any],
        update: AppSettingsUpdate,
    ) -> dict[str, Any]:
        next_settings = dict(current_settings or {})
        next_settings.update(dict(update.settings_patch or {}))
        return next_settings