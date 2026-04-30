"""Attachment processing — Core layer.

Handles Base64 encoding, MIME detection, and size validation
for file attachments. Extracted from UI layer so business logic
stays in Core.
"""
from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from typing import List

logger = logging.getLogger(__name__)

# Maximum file size for text attachments (1 MB)
MAX_TEXT_FILE_SIZE = 1024 * 1024

_IMAGE_MIME = {
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
    '.bmp': 'image/bmp',
}


@dataclass
class ProcessedAttachments:
    """Result of processing raw attachment paths."""
    encoded_images: List[str]      # data:mime;base64,... strings
    file_content_suffix: str       # text to append to user message


def process_attachments(attachments: List[dict]) -> ProcessedAttachments:
    """Process a list of attachment dicts (with 'path' and 'type' keys).

    Returns encoded images and file content text ready to be used
    in the message pipeline.
    """
    encoded_images: list[str] = []
    file_contents: list[str] = []

    for att in attachments:
        path = att.get('path', '')
        atype = att.get('type', '')

        if atype == 'image':
            img = _process_image(path)
            if img:
                encoded_images.append(img)
        else:
            text = _process_text_file(path)
            if text:
                file_contents.append(text)

    suffix = "".join(file_contents)
    return ProcessedAttachments(encoded_images=encoded_images, file_content_suffix=suffix)


def _process_image(path: str) -> str | None:
    """Encode an image file to a data URI, or return as-is if already encoded."""
    return encode_image_file_to_data_url(path)


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


def _process_text_file(path: str) -> str | None:
    """Read a text file for inclusion in the message."""
    try:
        size = os.path.getsize(path)
        if size > MAX_TEXT_FILE_SIZE:
            logger.info("File too large, skipping: %s (%d bytes)", path, size)
            return f"\n[File: {os.path.basename(path)} (Skipped: >{MAX_TEXT_FILE_SIZE // (1024*1024)}MB)]\n"
        try:
            with open(path, 'r', encoding='utf-8') as f:
                text = f.read()
            return f"\n\n--- File: {os.path.basename(path)} ---\n{text}\n--- End File ---"
        except UnicodeDecodeError:
            return f"\n[File: {os.path.basename(path)} (Binary content)]\n"
    except Exception as e:
        logger.warning("Error reading file %s: %s", path, e)
        return None
