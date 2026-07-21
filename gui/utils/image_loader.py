"""UI image loading utilities.

Supports:
- data URLs: data:<mime>;base64,<data>
- local file paths

Keeps logic centralized so widgets/dialogs stay small.
"""

from __future__ import annotations

import base64
from typing import Optional

from PyQt6.QtGui import QPixmap


def _safe_b64decode(data: str) -> Optional[bytes]:
    if not isinstance(data, str):
        return None
    data = data.strip()
    if not data:
        return None

    # Remove whitespace/newlines that sometimes appear in base64 blocks
    data = "".join(data.split())

    # Fix missing padding
    missing_padding = (-len(data)) % 4
    if missing_padding:
        data += "=" * missing_padding

    try:
        return base64.b64decode(data, validate=False)
    except Exception:
        return None


def load_pixmap(image_source: str) -> QPixmap:
    """Load a QPixmap from a data URL or local file path."""
    pixmap = QPixmap()

    if not isinstance(image_source, str) or not image_source:
        return pixmap

    try:
        if image_source.startswith("data:"):
            # data:image/png;base64,....
            if "," not in image_source:
                return pixmap
            _, data = image_source.split(",", 1)
            image_bytes = _safe_b64decode(data)
            if not image_bytes:
                return pixmap
            pixmap.loadFromData(image_bytes)
            return pixmap

        # Local file
        pixmap = QPixmap(image_source)
        return pixmap
    except Exception:
        return QPixmap()
