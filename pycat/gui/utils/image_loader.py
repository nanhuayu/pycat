"""UI image loading utilities.

Supports:
- data URLs: data:<mime>;base64,<data>
- local file paths

Keeps logic centralized so widgets/dialogs stay small.
"""

from __future__ import annotations

import base64
import gzip
from io import BytesIO
from pathlib import Path
import re
from urllib.parse import unquote_to_bytes
import xml.etree.ElementTree as ET
from typing import Optional

from PyQt6.QtCore import QBuffer, QIODevice, QRectF, QSize, Qt
from PyQt6.QtGui import QImage, QImageReader, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer


MAX_SVG_BYTES = 16 * 1024 * 1024


class _PassiveSVGTree(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise ValueError("SVG 不支持文档类型或实体声明")


def _svg_image(raw: bytes, max_size: QSize) -> QImage:
    if raw.startswith(b'\x1f\x8b'):
        with gzip.GzipFile(fileobj=BytesIO(raw)) as stream:
            raw = stream.read(MAX_SVG_BYTES + 1)
    if len(raw) > MAX_SVG_BYTES:
        raise ValueError("SVG 超过 16 MiB 预览限制")
    try:
        root = ET.fromstring(raw, parser=ET.XMLParser(target=_PassiveSVGTree()))
    except ET.ParseError as exc:
        raise ValueError("SVG 格式无效或文件已经损坏") from exc
    if root.tag.rsplit('}', 1)[-1] != 'svg':
        raise ValueError("SVG 格式无效")
    # Render a passive copy from bytes, never from a document base URL. Keep
    # internal references and embedded raster data; do not fetch external files.
    blocked = {'script', 'foreignObject', 'animate', 'animateMotion', 'animateTransform', 'set'}
    urls = re.compile(r'url\(\s*([\"\']?)(.*?)\1\s*\)', re.I)
    for node in root.iter():
        for child in list(node):
            if child.tag.rsplit('}', 1)[-1] in blocked:
                node.remove(child)
        if node.tag.rsplit('}', 1)[-1] == 'style' and node.text:
            node.text = re.sub(r'@import\b[^;]*;?', '', node.text, flags=re.I)
            node.text = urls.sub(lambda m: m[0] if m[2].strip().startswith('#') else 'none', node.text)
        for key, value in list(node.attrib.items()):
            name = key.rsplit('}', 1)[-1].lower()
            allowed_href = value.startswith('#') or re.match(r'^data:image/(png|jpeg|gif|webp);base64,', value)
            if (name.startswith('on') or name == 'base' or name == 'href' and not allowed_href
                    or any(not m[2].strip().startswith('#') for m in urls.finditer(value))):
                del node.attrib[key]
    renderer = QSvgRenderer(ET.tostring(root))
    if not renderer.isValid():
        raise ValueError("SVG 无法解析")
    size = renderer.defaultSize()
    if not size.isValid() or size.isEmpty():
        size = renderer.viewBoxF().size().toSize()
    if not size.isValid() or size.isEmpty():
        size = QSize(300, 150)
    if size.width() > max_size.width() or size.height() > max_size.height():
        size = size.scaled(max_size, Qt.AspectRatioMode.KeepAspectRatio)
    size = QSize(max(1, size.width()), max(1, size.height()))
    image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        renderer.render(painter, QRectF(0, 0, size.width(), size.height()))
    finally:
        painter.end()
    return image


def read_image(source: str, *, max_size: QSize | None = None) -> QImage:
    """Decode bounded native raster/SVG previews; no browser or external fetches."""
    limit = max_size or QSize(4096, 4096)
    buffer = None
    raw = None
    if source.startswith('data:'):
        header, _, payload = source.partition(',')
        raw = _safe_b64decode(payload) if ';base64' in header else unquote_to_bytes(payload)
        if not raw:
            raise ValueError("图片数据为空")
        svg = 'image/svg+xml' in header
    else:
        svg = Path(source).suffix.lower() in {'.svg', '.svgz'}
        if svg:
            with Path(source).open('rb') as stream:
                raw = stream.read(MAX_SVG_BYTES + 1)
    if svg:
        return _svg_image(raw, limit)
    if raw is not None:
        buffer = QBuffer()
        buffer.setData(raw)
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
    else:
        reader = QImageReader(source)
    reader.setAutoTransform(True)
    if bytes(reader.format()) in {b'svg', b'svgz'}:
        if raw is None:
            with Path(source).open('rb') as stream:
                raw = stream.read(MAX_SVG_BYTES + 1)
        return _svg_image(raw, limit)
    size = reader.size()
    if not size.isValid():
        raise ValueError("图片无法解析")
    if size.width() > limit.width() or size.height() > limit.height():
        reader.setScaledSize(size.scaled(limit, Qt.AspectRatioMode.KeepAspectRatio))
    result = reader.read()
    if result.isNull():
        raise ValueError("图片无法解析或超过解码预算")
    return result


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


def load_pixmap(image_source: str, *, max_size: QSize | None = None) -> QPixmap:
    """Load a QPixmap, optionally asking the decoder for a bounded size."""
    if not isinstance(image_source, str) or not image_source:
        return QPixmap()
    try:
        return QPixmap.fromImage(read_image(image_source, max_size=max_size))
    except Exception:
        return QPixmap()
