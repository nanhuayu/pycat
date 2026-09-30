"""Bounded raster validation shared by image model inputs and outputs."""

from __future__ import annotations

import base64
import warnings
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps

MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_INPUT_IMAGES = 16
VIEW_IMAGE_MAX_SIDE = 2048
MAX_VIEW_IMAGE_BYTES = 4 * 1024 * 1024


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


def prepare_view_image(data: bytes, *, detail: str = 'auto') -> tuple[RasterImage, dict]:
    """Create a bounded model view without changing source bytes or summarizing pixels."""
    if detail not in {'auto', 'original'}:
        raise ValueError('detail must be auto or original.')
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError('Image is empty or larger than 25 MiB.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as raw:
                if raw.width * raw.height > MAX_IMAGE_PIXELS:
                    raise ValueError('Image exceeds 40 million pixels; use a smaller source or crop.')
                if getattr(raw, 'is_animated', False) or getattr(raw, 'n_frames', 1) != 1:
                    raise ValueError('Use a single static image; animations and multi-frame images are not supported.')
                original_width, original_height = raw.size
                oriented = ImageOps.exif_transpose(raw)
                oriented_size = oriented.size
                if detail == 'auto':
                    oriented.thumbnail((VIEW_IMAGE_MAX_SIDE, VIEW_IMAGE_MAX_SIDE), Image.Resampling.LANCZOS)
                resized = oriented.size != oriented_size
                # Reuse supported, already bounded bytes when no transformation is needed.
                mime = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp'}.get(raw.format or '')
                if mime and not resized and raw.getexif().get(274, 1) == 1 and len(data) <= MAX_VIEW_IMAGE_BYTES:
                    encoded = data
                else:
                    stream = BytesIO()
                    if raw.format == 'JPEG':
                        oriented.convert('RGB').save(stream, format='JPEG', quality=90)
                        mime = 'image/jpeg'
                    else:
                        mode = 'RGBA' if 'A' in oriented.getbands() or 'transparency' in oriented.info else 'RGB'
                        oriented.convert(mode).save(stream, format='PNG')
                        mime = 'image/png'
                    encoded = stream.getvalue()
                if len(encoded) > MAX_VIEW_IMAGE_BYTES:
                    raise ValueError('Image view exceeds 4 MiB; use a smaller source or crop. No further downscaling was applied.')
                raster = RasterImage(encoded, mime, oriented.width, oriented.height,
                                     'A' in oriented.getbands() or 'transparency' in oriented.info)
                return raster, {
                    'original_width': original_width, 'original_height': original_height,
                    'width': raster.width, 'height': raster.height, 'resized': resized,
                    'mime': mime, 'bytes': len(encoded),
                }
    except (OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError('Unsupported or damaged raster image; render SVG explicitly before viewing.') from exc


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
