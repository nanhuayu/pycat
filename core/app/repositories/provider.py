from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from models.provider import PROVIDER_SCHEMA_VERSION, Provider


logger = logging.getLogger(__name__)


class ProviderRepository:
    """File repository for LLM provider catalog."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.providers_file = self.data_dir / "providers.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._loaded_schema_version = PROVIDER_SCHEMA_VERSION

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
                return [Provider.from_dict(item) for item in data or [] if isinstance(item, dict)]
        except Exception as exc:
            logger.warning("Error loading providers: %s", exc)
        return []

    def save(self, providers: list[Provider]) -> bool:
        temp_name = ""
        try:
            payload = {
                "schema_version": PROVIDER_SCHEMA_VERSION,
                "providers": [provider.to_dict() for provider in providers],
            }
            fd, temp_name = tempfile.mkstemp(
                prefix=".providers.",
                suffix=".tmp",
                dir=str(self.data_dir),
                text=True,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False, indent=2))
            os.replace(temp_name, self.providers_file)
            self._loaded_schema_version = PROVIDER_SCHEMA_VERSION
            return True
        except Exception as exc:
            logger.warning("Error saving providers: %s", exc)
            try:
                if temp_name:
                    Path(temp_name).unlink(missing_ok=True)
            except Exception:
                pass
            return False
