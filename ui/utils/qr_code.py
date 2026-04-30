from __future__ import annotations

from io import BytesIO

from PIL import Image
from PyQt6.QtGui import QPixmap

try:
    import qrcode
except Exception:  # pragma: no cover - optional dependency fallback
    qrcode = None


def _resampling_nearest() -> int:
    resampling = getattr(Image, "Resampling", None)
    if resampling is not None:
        return int(resampling.NEAREST)
    return int(getattr(Image, "NEAREST", 0))


def build_qr_code_pixmap(content: str, *, size: int = 220) -> QPixmap:
    """Render QR content into a Qt pixmap with a white canvas."""

    text = str(content or "").strip()
    if not text or qrcode is None:
        return QPixmap()

    qr = qrcode.QRCode(border=2, box_size=10)
    qr.add_data(text)
    qr.make(fit=True)

    image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    target_size = max(96, int(size or 220))
    image.thumbnail((target_size, target_size), _resampling_nearest())

    canvas = Image.new("RGB", (target_size, target_size), "white")
    offset_x = (target_size - image.size[0]) // 2
    offset_y = (target_size - image.size[1]) // 2
    canvas.paste(image, (offset_x, offset_y))

    buffer = BytesIO()
    canvas.save(buffer, format="PNG")
    pixmap = QPixmap()
    pixmap.loadFromData(buffer.getvalue(), "PNG")
    return pixmap