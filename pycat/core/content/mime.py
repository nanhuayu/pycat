"""Portable MIME inference and text classification for PyCat content.

Host registries disagree (Windows maps ``.md`` to nothing, ``.ts`` to MPEG
video and ``.csv`` to Excel), so formats PyCat previews are pinned here
before falling back to :mod:`mimetypes`.
"""
from __future__ import annotations

import mimetypes
import os
from pathlib import PurePath

DEFAULT_MIME = "application/octet-stream"

_SUFFIX_MIME = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".ini": "text/plain",
    ".cfg": "text/plain",
    ".bat": "text/plain",
    ".ps1": "text/plain",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".py": "text/x-python",
    ".pyw": "text/x-python",
    ".js": "text/javascript",
    ".jsx": "text/javascript",
    ".ts": "text/x-typescript",
    ".tsx": "text/x-typescript",
    ".css": "text/css",
    ".html": "text/html",
    ".htm": "text/html",
    ".sql": "text/x-sql",
    ".sh": "text/x-shellscript",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
}
_TEXT_APPLICATION_MIMES = {
    "application/json",
    "application/x-ndjson",
    "application/xml",
    "application/javascript",
    "application/yaml",
    "application/x-yaml",
    "application/toml",
}


def guess_mime(name: str | os.PathLike[str]) -> str:
    """Infer a MIME type from a file name; unknown names are octet streams."""
    text = os.fspath(name)
    pinned = _SUFFIX_MIME.get(PurePath(text).suffix.lower())
    return pinned or mimetypes.guess_type(text)[0] or DEFAULT_MIME


def is_text_mime(mime: str) -> bool:
    normalized = str(mime or "").split(";", 1)[0].strip().lower()
    return normalized.startswith("text/") or normalized in _TEXT_APPLICATION_MIMES
