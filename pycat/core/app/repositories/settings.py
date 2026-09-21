from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


class SettingsRepository:
    """File repository for application settings."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.settings_file = self.data_dir / "settings.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        try:
            if self.settings_file.exists():
                return json.loads(self.settings_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Error loading settings: %s", exc)
        return {}

    def save(self, settings: dict[str, Any]) -> bool:
        try:
            self.settings_file.write_text(json.dumps(settings or {}, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except Exception as exc:
            logger.warning("Error saving settings: %s", exc)
            return False
