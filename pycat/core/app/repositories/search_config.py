from __future__ import annotations

import json
import logging
from pathlib import Path

from pycat.models.search_config import SearchConfig


logger = logging.getLogger(__name__)


class SearchConfigRepository:
    """File repository for web search configuration."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.search_config_file = self.data_dir / "search_config.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> SearchConfig:
        try:
            if self.search_config_file.exists():
                return SearchConfig.from_dict(json.loads(self.search_config_file.read_text(encoding="utf-8")))
        except Exception as exc:
            logger.warning("Error loading search config: %s", exc)
        return SearchConfig()

    def save(self, config: SearchConfig) -> bool:
        try:
            self.search_config_file.write_text(json.dumps(config.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except Exception as exc:
            logger.warning("Error saving search config: %s", exc)
            return False
