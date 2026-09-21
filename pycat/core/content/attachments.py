"""Shared composer-text and image data-URL helpers."""
from __future__ import annotations

import base64
import logging
import os
from typing import Any, Mapping

logger = logging.getLogger(__name__)

_IMAGE_MIME = {
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
    '.bmp': 'image/bmp',
}


def extract_composer_text(content: Any, metadata: Mapping[str, Any] | None = None) -> str:
    """Return the typed prompt without persisted text-attachment bodies."""

    meta = metadata if isinstance(metadata, Mapping) else {}
    original = meta.get("composer_text")
    if isinstance(original, str):
        return original.strip()

    text = str(content or "")
    markers = [
        index
        for marker in ("\n\n--- File:", "\n[File:")
        if (index := text.find(marker)) >= 0
    ]
    if markers:
        text = text[: min(markers)]
    return text.strip()


def encode_image_file_to_data_url(path: str) -> str | None:
    """Encode an image file path to the shared data URL format."""
    if isinstance(path, str) and path.startswith('data:'):
        return path
    try:
        with open(path, 'rb') as f:
            data = base64.b64encode(f.read()).decode('utf-8')
        ext = os.path.splitext(path)[1].lower()
        mime = _IMAGE_MIME.get(ext, 'image/png')
        return f"data:{mime};base64,{data}"
    except Exception as e:
        logger.warning("Error loading image %s: %s", path, e)
        return None
