"""Portable names for files PyCat writes from untrusted or remote input."""
from __future__ import annotations

import re
from pathlib import PureWindowsPath

_UNSAFE_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(value: object, default: str = "file", *, limit: int = 180) -> str:
    """Last path segment made valid on Windows and POSIX; never empty, ``..`` or a device name."""
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_CHARACTERS.sub("_", name).strip()[:limit].rstrip(" .")
    if not name:
        return default
    return f"_{name}" if PureWindowsPath(name).is_reserved() else name
