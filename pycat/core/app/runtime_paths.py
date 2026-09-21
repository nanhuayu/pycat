"""Small runtime path helpers shared by CLI and UI presenters."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def get_debug_log_path(app_settings: dict[str, Any] | None, data_dir: Any) -> str | None:
    """Return the stream debug log path when stream logging is enabled."""
    if not bool((app_settings or {}).get("log_stream", False)):
        return None
    try:
        return str(Path(data_dir) / "stream_debug.log")
    except Exception as exc:
        logger.debug("Failed to construct debug log path: %s", exc)
        return None
