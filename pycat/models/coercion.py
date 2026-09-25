"""Tolerant boolean parsing for persisted settings and user-edited metadata."""
from __future__ import annotations

from typing import Any

_TRUE_TEXT = frozenset({"1", "true", "yes", "on"})
_FALSE_TEXT = frozenset({"0", "false", "no", "off"})


def optional_bool(value: Any) -> bool | None:
    """Parse a boolean; ``"false"``/``"0"`` are false, empty or unknown text is ``None``."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE_TEXT:
        return True
    if text in _FALSE_TEXT:
        return False
    return None


def as_bool(value: Any, default: bool = False) -> bool:
    parsed = optional_bool(value)
    return default if parsed is None else parsed
