from __future__ import annotations

import json
import logging
from pathlib import Path

from pycat.core.persistence import atomic_write_text
from pycat.models.provider import PROVIDER_SCHEMA_VERSION, Provider

logger = logging.getLogger(__name__)


class ProviderRepository:
    """File repository for LLM provider catalog."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.providers_file = self.data_dir / "providers.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._loaded_schema_version = PROVIDER_SCHEMA_VERSION
        self._has_catalog = False

    @property
    def has_catalog(self) -> bool:
        """Whether a valid provider catalog has already been written."""

        return self._has_catalog

    @property
    def catalog_file_exists(self) -> bool:
        """Whether a user catalog file exists, even if it cannot be parsed."""

        return self.providers_file.exists()

    @property
    def needs_migration(self) -> bool:
        return self._loaded_schema_version < PROVIDER_SCHEMA_VERSION

    def load(self) -> list[Provider]:
        try:
            if self.providers_file.exists():
                payload = json.loads(self.providers_file.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    self._loaded_schema_version = int(payload.get("schema_version", 1) or 1)
                    data = payload.get("providers", [])
                else:
                    self._loaded_schema_version = 1
                    data = payload
                self._has_catalog = isinstance(data, list)
                return [Provider.from_dict(item) for item in data or [] if isinstance(item, dict)]
        except Exception as exc:
            self._has_catalog = False
            logger.warning("Error loading providers: %s", exc)
        return []

    def save(self, providers: list[Provider]) -> bool:
        try:
            payload = {
                "schema_version": PROVIDER_SCHEMA_VERSION,
                "providers": [provider.to_dict() for provider in providers],
            }
            atomic_write_text(self.providers_file, json.dumps(payload, ensure_ascii=False, indent=2))
            self._loaded_schema_version = PROVIDER_SCHEMA_VERSION
            self._has_catalog = True
            return True
        except Exception as exc:
            logger.warning("Error saving providers: %s", exc)
            return False
