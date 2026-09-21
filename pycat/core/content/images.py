"""Bounded raster validation shared by image model inputs and outputs."""

from __future__ import annotations

import base64
import warnings
from dataclasses import dataclass
from io import BytesIO

from PIL import Image

MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_INPUT_IMAGES = 16


@dataclass(frozen=True)
class RasterImage:
    data: bytes
    mime: str
    width: int
    height: int
    alpha: bool

    @property
    def data_url(self) -> str:
        return f"data:{self.mime};base64," + base64.b64encode(self.data).decode("ascii")


def inspect_image(data: bytes) -> RasterImage:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("图片为空或超过 25 MiB。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(image.format or "")
                if not mime or getattr(image, "is_animated", False):
                    raise ValueError("需要静态 PNG、JPEG 或 WebP 图片。")
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise ValueError("图片像素超过 4000 万上限。")
                result = RasterImage(
                    data, mime, image.width, image.height, "A" in image.getbands() or "transparency" in image.info
                )
                image.verify()
                return result
    except (OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("图片损坏或无法安全解码。") from exc


def decode_image(value: str) -> RasterImage:
    prefix, separator, encoded = str(value or "").partition(",")
    if not separator or not prefix.startswith("data:image/") or not prefix.endswith(";base64"):
        raise ValueError("需要已解析的图片内容，不能使用未验证的 URL 或路径。")
    if len(encoded) > MAX_IMAGE_BYTES * 4 // 3 + 4:
        raise ValueError("图片超过 25 MiB。")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ValueError("图片 base64 无效。") from exc
    image = inspect_image(raw)
    if prefix[5:-7] != image.mime:
        raise ValueError("图片声明格式与实际内容不一致。")
    return image
