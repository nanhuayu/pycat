"""UI image helpers.

Centralizes:
- Extracting images from QMimeData (paste/drag-drop)
- Converting QImage/QPixmap to data URLs
- Filtering supported image file paths

Keeping this logic here prevents widget code from growing complex.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Iterable, Tuple, List, Optional

from PyQt6.QtCore import QByteArray, QBuffer, QIODevice
from PyQt6.QtGui import QImage, QPixmap, QGuiApplication

# Re-export for convenience (actual implementation lives in core attachments)
from core.attachments import encode_image_file_to_data_url


logger = logging.getLogger(__name__)


_SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def is_supported_image_path(path: str) -> bool:
    if not isinstance(path, str) or not path:
        return False
    _, ext = os.path.splitext(path)
    return ext.lower() in _SUPPORTED_EXTS


def qimage_to_data_url(image: object, mime: str = "image/png") -> Optional[str]:
    """Convert a QImage/QPixmap (or image-like) into a data URL."""
    qimg: Optional[QImage] = None

    if isinstance(image, QImage):
        qimg = image
    elif isinstance(image, QPixmap):
        qimg = image.toImage()

    if qimg is None or qimg.isNull():
        return None

    # Encode as PNG to keep it lossless and widely supported.
    ba = QByteArray()
    buffer = QBuffer(ba)
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        return None

    try:
        ok = qimg.save(buffer, "PNG")
    finally:
        buffer.close()

    if not ok:
        return None

    raw = bytes(ba)
    b64 = base64.b64encode(raw).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def extract_images_from_mime(mime_data: object) -> Tuple[List[str], List[str]]:
    """Return (data_urls, file_paths) extracted from QMimeData."""
    data_urls: List[str] = []
    file_paths: List[str] = []

    if mime_data is None:
        return data_urls, file_paths

    try:
        if hasattr(mime_data, "hasImage") and mime_data.hasImage():
            try:
                data = mime_data.imageData()
                url = qimage_to_data_url(data)
                if url:
                    data_urls.append(url)
            except Exception as exc:
                logger.debug("Failed to extract embedded image from mime data: %s", exc)

        if hasattr(mime_data, "hasUrls") and mime_data.hasUrls():
            try:
                for u in mime_data.urls() or []:
                    p = u.toLocalFile()
                    if is_supported_image_path(p):
                        file_paths.append(p)
            except Exception as exc:
                logger.debug("Failed to extract image file paths from mime data: %s", exc)
    except Exception as exc:
        logger.debug("Failed to inspect mime data for images: %s", exc)
        return data_urls, file_paths

    return data_urls, file_paths


def extract_images_from_clipboard() -> List[str]:
    """Convenience: get screenshot image from clipboard as data URLs."""
    try:
        cb = QGuiApplication.clipboard()
        md = cb.mimeData()
        data_urls, file_paths = extract_images_from_mime(md)
        # Prefer embedded image data; also accept file paths if clipboard provides them.
        return data_urls + file_paths
    except Exception as exc:
        logger.debug("Failed to extract clipboard images: %s", exc)
        return []
