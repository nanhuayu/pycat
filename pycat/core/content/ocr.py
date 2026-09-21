from __future__ import annotations

import asyncio
import base64
import hashlib
import threading
import time
import warnings
from io import BytesIO
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from importlib.util import find_spec
from PIL import Image, ImageOps, UnidentifiedImageError

from pycat.core.capabilities.executor import CapabilityRunContext
from pycat.models.contracts.capability import CapabilitiesConfig
from pycat.models.contracts.config import OcrConfig


_DET_MODEL = "PP-OCRv6_det_small.onnx"
_REC_MODEL = "PP-OCRv6_rec_small.onnx"
_ENGINE_ID = "ppocrv6-small-onnx"
_SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
_IMAGE_MAX_SIDE = 2000
_PDF_MIME_TYPES = {"application/pdf", "application/x-pdf"}


@dataclass(frozen=True)
class OcrBlock:
    text: str
    score: float | None = None
    box: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class _OcrPage:
    number: int
    blocks: tuple[OcrBlock, ...] = ()

    @property
    def text(self) -> str:
        return "\n".join(block.text for block in self.blocks if str(block.text or "").strip())


@dataclass(frozen=True)
class _OcrResult:
    source_ref: str
    source_digest: str
    engine: str
    total_pages: int
    processed_pages: tuple[int, ...]
    pages: tuple[_OcrPage, ...]
    next_page: int | None = None
    has_more: bool = False
    elapsed_ms: int = 0

    @property
    def block_count(self) -> int:
        return sum(len(page.blocks) for page in self.pages)

    @property
    def average_confidence(self) -> float | None:
        scores = [block.score for page in self.pages for block in page.blocks if block.score is not None]
        return round(sum(scores) / len(scores), 5) if scores else None

    @property
    def no_text(self) -> bool:
        return self.block_count == 0

    def to_text(self) -> str:
        page_range = ""
        if self.processed_pages:
            page_range = f"{self.processed_pages[0]}-{self.processed_pages[-1]}"
        next_page = self.next_page if self.next_page is not None else "none"
        lines = [
            f"OCR source={self.source_ref}",
            (
                f"engine={self.engine} pages={page_range or 'none'} "
                f"total_pages={self.total_pages} next_page={next_page}"
            ),
            "",
        ]
        for page in self.pages:
            lines.extend((f"[page {page.number}]", page.text or "[no text detected]", ""))
        return "\n".join(lines).rstrip()

    def metadata(self) -> dict[str, Any]:
        return {
            "source_ref": self.source_ref,
            "source_digest": self.source_digest,
            "engine": self.engine,
            "processed_pages": list(self.processed_pages),
            "total_pages": int(self.total_pages),
            "next_page": self.next_page,
            "has_more": bool(self.has_more),
            "block_count": int(self.block_count),
            "no_text": bool(self.no_text),
            "average_confidence": self.average_confidence,
            "elapsed_ms": int(self.elapsed_ms),
        }


@dataclass(frozen=True)
class OcrStatus:
    available: bool
    detail: str = ""
    missing: tuple[str, ...] = field(default_factory=tuple)


class OcrError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = str(code or "ocr_failed")
        super().__init__(str(message or self.code))


class OcrService:
    """OCR workflow shared by files, PDF pages and desktop capture."""

    def __init__(
        self,
        config: OcrConfig | None = None,
        *,
        assets_dir: str | Path | None = None,
        engine_factory: Callable[[], Any] | None = None,
        capability_executor: Any = None,
    ) -> None:
        self._config = config or OcrConfig()
        self._capability_executor = capability_executor
        self._config_lock = threading.Lock()
        self._engine_factory = engine_factory
        self._custom_engine_factory = engine_factory is not None
        self._engine: Any = None
        self._engine_lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self.assets_dir = Path(assets_dir) if assets_dir is not None else self.default_assets_dir()


    @staticmethod
    def default_assets_dir() -> Path:
        return Path(__file__).resolve().parents[2] / "assets" / "ocr" / "ppocrv6-small"


    @property
    def config(self) -> OcrConfig:
        with self._config_lock:
            return self._config


    def update_configuration(self, config: OcrConfig | None) -> None:
        with self._config_lock:
            self._config = config or OcrConfig()


    def status(self) -> OcrStatus:
        if self.config.backend == "vision":
            try:
                _capability, selection = self._vision_selection()
                return OcrStatus(True, f"视觉模型：{selection.provider.name}|{selection.model}")
            except OcrError as exc:
                return OcrStatus(False, str(exc))
        if self._custom_engine_factory:
            return OcrStatus(available=True, detail="OCR 引擎可用")

        missing: list[str] = [name for name in ("numpy", "onnxruntime", "cv2", "pyclipper") if find_spec(name) is None]
        for filename in (_DET_MODEL, _REC_MODEL):
            path = self.assets_dir / filename
            try:
                valid = path.is_file() and path.stat().st_size > 0
            except OSError:
                valid = False
            if not valid:
                missing.append(filename)

        available = not missing
        detail = "本地 PP-OCRv6 Small 组件完整" if available else "缺少：" + "、".join(missing)
        return OcrStatus(
            available=available,
            detail=detail,
            missing=tuple(missing),
        )


    def is_available(self) -> bool:
        return bool(self.config.enabled and self.status().available)


    async def recognize(
        self,
        path: str | Path,
        *,
        source_ref: str,
        source_digest: str = "",
        source_name: str = "",
        source_mime: str = "",
        start_page: int = 1,
        page_count: int | None = None,
        cancel_event: Any = None,
    ) -> _OcrResult:
        config = self.config
        if not config.enabled:
            raise OcrError("ocr_disabled", "OCR 已在设置中关闭。")
        status = self.status()
        if not status.available:
            raise OcrError("ocr_unavailable", status.detail)
        try:
            normalized_start = int(start_page)
            normalized_count = config.pdf_batch_pages if page_count is None else int(page_count)
        except (TypeError, ValueError) as exc:
            raise OcrError("invalid_argument", "页码参数必须是整数。") from exc
        if normalized_start < 1:
            raise OcrError("page_out_of_range", "start_page 必须从 1 开始。")
        if normalized_count < 1 or normalized_count > 32:
            raise OcrError("invalid_argument", "page_count 必须在 1 到 32 之间。")

        if config.backend == "vision":
            return await self._recognize_vision(Path(path), source_ref=source_ref, source_digest=source_digest,
                source_name=source_name or Path(path).name, source_mime=source_mime,
                start_page=normalized_start, page_count=normalized_count, cancel_event=cancel_event)

        return await asyncio.to_thread(
            self._recognize_sync,
            Path(path),
            str(source_ref or ""),
            str(source_digest or ""),
            str(source_name or Path(path).name),
            str(source_mime or ""),
            normalized_start,
            normalized_count,
            cancel_event,
        )


    def _recognize_sync(
        self,
        path: Path,
        source_ref: str,
        source_digest: str,
        source_name: str,
        source_mime: str,
        start_page: int,
        page_count: int,
        cancel_event: Any,
    ) -> _OcrResult:
        self._check_cancel(cancel_event)
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise OcrError("source_not_found", "OCR 源文件不存在。")

        before = self._stat_signature(resolved)
        digest = self._sha256(resolved, cancel_event)
        if source_digest and digest != source_digest:
            raise OcrError("source_changed", "OCR 源文件与输入引用不一致，请重新附加后重试。")
        if before != self._stat_signature(resolved):
            raise OcrError("source_changed", "文件在 OCR 准备期间发生变化，请重试。")

        started = time.perf_counter()
        content_kind = self._classify_source(
            resolved,
            source_name=source_name,
            source_mime=source_mime,
        )
        if content_kind == "pdf":
            total_pages, pages = self._recognize_pdf(
                resolved,
                start_page=start_page,
                page_count=page_count,
                cancel_event=cancel_event,
            )
        elif content_kind == "image":
            image = self._load_image(resolved)
            self._check_cancel(cancel_event)
            pages = (_OcrPage(number=1, blocks=self._recognize_image(image, cancel_event)),)
            total_pages = 1
        else:
            raise OcrError("unsupported_type", "首版本地 OCR 仅支持静态图片和 PDF。")

        if before != self._stat_signature(resolved):
            raise OcrError("source_changed", "文件在 OCR 处理期间发生变化，请重试。")
        if digest != self._sha256(resolved, cancel_event) or before != self._stat_signature(resolved):
            raise OcrError("source_changed", "文件在 OCR 处理期间发生变化，请重试。")

        processed_pages = tuple(page.number for page in pages)
        last_page = processed_pages[-1] if processed_pages else start_page - 1
        has_more = last_page < total_pages
        return _OcrResult(
            source_ref=source_ref,
            source_digest=digest,
            engine=str(getattr(self._get_engine(), "revision", _ENGINE_ID)),
            total_pages=total_pages,
            processed_pages=processed_pages,
            pages=pages,
            next_page=(last_page + 1) if has_more else None,
            has_more=has_more,
            elapsed_ms=max(0, round((time.perf_counter() - started) * 1000)),
        )


    def _recognize_pdf(
        self,
        path: Path,
        *,
        start_page: int,
        page_count: int,
        cancel_event: Any,
    ) -> tuple[int, tuple[_OcrPage, ...]]:
        import numpy as np  # optional OCR raster backend
        import pymupdf
        try:
            document = pymupdf.open(str(path))
        except Exception as exc:
            raise OcrError("pdf_invalid", "PDF 无法打开或文件已损坏。") from exc

        pages: list[_OcrPage] = []
        try:
            if bool(getattr(document, "needs_pass", False)):
                raise OcrError("pdf_encrypted", "PDF 已加密，请先解密后重试。")
            total_pages = int(document.page_count)
            if start_page > total_pages or total_pages <= 0:
                raise OcrError(
                    "page_out_of_range",
                    f"start_page 超出范围，PDF 共 {total_pages} 页。",
                )
            end_page = min(total_pages, start_page + page_count - 1)
            for page_number in range(start_page, end_page + 1):
                self._check_cancel(cancel_event)
                page = document.load_page(page_number - 1)
                rect = page.rect
                longest = max(float(rect.width), float(rect.height), 1.0)
                scale = _IMAGE_MAX_SIDE / longest
                pixmap = page.get_pixmap(
                    matrix=pymupdf.Matrix(scale, scale),
                    colorspace=pymupdf.csRGB,
                    alpha=False,
                )
                try:
                    rgb = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
                        pixmap.height,
                        pixmap.width,
                        pixmap.n,
                    )
                    image = rgb[:, :, :3][:, :, ::-1].copy()
                finally:
                    pixmap = None
                    page = None
                blocks = self._recognize_image(image, cancel_event)
                pages.append(_OcrPage(number=page_number, blocks=blocks))
        except OcrError:
            raise
        except MemoryError as exc:
            raise OcrError(
                "image_decode_resource_exceeded",
                "PDF 页面无法在当前可用内存中安全渲染。",
            ) from exc
        except Exception as exc:
            raise OcrError("inference_failed", "PDF 页面 OCR 失败。") from exc
        finally:
            document.close()
        return total_pages, tuple(pages)


    def _load_image(self, path: Path | BytesIO):
        import numpy as np  # optional OCR raster backend
        with self._load_pil_image(path) as image:
            return np.asarray(image)[:, :, ::-1].copy()

    def _load_pil_image(self, path: Path | BytesIO):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as raw:
                    if bool(getattr(raw, "is_animated", False)) or int(getattr(raw, "n_frames", 1)) != 1:
                        raise OcrError("unsupported_type", "首版 OCR 不支持动画或多页图片。")
                    if str(getattr(raw, "format", "") or "").upper() in {"JPEG", "MPO"}:
                        raw.draft("RGB", (_IMAGE_MAX_SIDE, _IMAGE_MAX_SIDE))
                    image = ImageOps.exif_transpose(raw)
                    image.thumbnail((_IMAGE_MAX_SIDE, _IMAGE_MAX_SIDE), Image.Resampling.LANCZOS)
                    rgb = image.convert("RGB")
                    return rgb
        except OcrError:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning, MemoryError) as exc:
            raise OcrError(
                "image_decode_resource_exceeded",
                "图片无法在当前可用内存中安全解码。",
            ) from exc
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise OcrError("unsupported_type", "图片格式不受支持或文件已损坏。") from exc


    def _recognize_image(self, image: Any, cancel_event: Any) -> tuple[OcrBlock, ...]:
        self._check_cancel(cancel_event)
        engine = self._get_engine()
        try:
            with self._inference_lock:
                self._check_cancel(cancel_event)
                raw_blocks = engine.recognize(image)
        except OcrError:
            raise
        except MemoryError as exc:
            raise OcrError("image_decode_resource_exceeded", "OCR 推理内存不足。") from exc
        except Exception as exc:
            raise OcrError("inference_failed", "PP-OCRv6 推理失败。") from exc
        self._check_cancel(cancel_event)

        blocks: list[OcrBlock] = []
        for item in raw_blocks or ():
            if isinstance(item, OcrBlock):
                block = item
            elif isinstance(item, dict):
                block = OcrBlock(
                    text=str(item.get("text") or ""),
                    score=float(item.get("score") or 0.0),
                    box=self._normalize_box(item.get("box")),
                )
            else:
                block = OcrBlock(
                    text=str(getattr(item, "text", "") or ""),
                    score=float(getattr(item, "score", 0.0) or 0.0),
                    box=self._normalize_box(getattr(item, "box", ())),
                )
            if block.text.strip():
                blocks.append(block)
        return tuple(blocks)


    def _get_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        with self._engine_lock:
            if self._engine is None:
                factory = self._engine_factory or self._create_default_engine
                try:
                    self._engine = factory()
                except OcrError:
                    raise
                except Exception as exc:
                    raise OcrError("model_load_failed", "PP-OCRv6 模型加载失败。") from exc
        return self._engine


    def _create_default_engine(self) -> Any:
        # Keep OpenCV out of GUI startup; importing it first can shadow PyQt DLLs on Windows.
        from pycat.core.content.ppocrv6 import PpOcrV6Engine

        return PpOcrV6Engine(
            detection_model=self.assets_dir / _DET_MODEL,
            recognition_model=self.assets_dir / _REC_MODEL,
        )


    @staticmethod
    def _normalize_box(value: Any) -> tuple[tuple[int, int], ...]:
        points: list[tuple[int, int]] = []
        for point in value or ():
            try:
                points.append((int(round(float(point[0]))), int(round(float(point[1])))))
            except (IndexError, TypeError, ValueError):
                continue
        return tuple(points)


    @staticmethod
    def _check_cancel(cancel_event: Any) -> None:
        if cancel_event is not None and hasattr(cancel_event, "is_set") and cancel_event.is_set():
            raise OcrError("cancelled", "OCR 已取消。")


    @staticmethod
    def _stat_signature(path: Path) -> tuple[int, int]:
        try:
            stat = path.stat()
        except OSError as exc:
            raise OcrError("source_not_found", "OCR 源文件不存在。") from exc
        return int(stat.st_size), int(stat.st_mtime_ns)


    @classmethod
    def _sha256(cls, path: Path, cancel_event: Any) -> str:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                while True:
                    cls._check_cancel(cancel_event)
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
        except OcrError:
            raise
        except OSError as exc:
            raise OcrError("source_not_found", "OCR 源文件无法读取。") from exc
        return digest.hexdigest()


    @classmethod
    def _classify_source(
        cls,
        path: Path,
        *,
        source_name: str,
        source_mime: str,
    ) -> str:
        """Classify an OCR source using declarations plus a bounded signature read."""
        name_kind = cls._declared_name_kind(source_name)
        mime_kind = cls._declared_mime_kind(source_mime)
        if name_kind and mime_kind and name_kind != mime_kind:
            raise OcrError(
                "content_type_conflict",
                "文件名称与声明的 MIME 类型不一致，请重新附加正确文件。",
            )

        signature_kind = cls._signature_kind(path)
        declared_kind = mime_kind or name_kind
        if signature_kind and declared_kind and signature_kind != declared_kind:
            raise OcrError(
                "content_type_conflict",
                "文件内容与声明的文件类型不一致，请检查原文件。",
            )
        return signature_kind or declared_kind or "unsupported"


    @staticmethod
    def _declared_name_kind(name: str) -> str:
        suffix = Path(str(name or "")).suffix.lower()
        if suffix == ".pdf":
            return "pdf"
        if suffix in _SUPPORTED_IMAGE_SUFFIXES:
            return "image"
        return ""


    @staticmethod
    def _declared_mime_kind(mime: str) -> str:
        normalized = str(mime or "").split(";", 1)[0].strip().lower()
        if normalized in _PDF_MIME_TYPES:
            return "pdf"
        if normalized.startswith("image/"):
            return "image"
        return ""


    @staticmethod
    def _signature_kind(path: Path) -> str:
        try:
            with path.open("rb") as stream:
                header = stream.read(16)
        except OSError as exc:
            raise OcrError("source_not_found", "OCR 源文件无法读取。") from exc
        if header.startswith(b"%PDF-"):
            return "pdf"
        if (
            header.startswith(b"\x89PNG\r\n\x1a\n")
            or header.startswith(b"\xff\xd8\xff")
            or header.startswith(b"BM")
            or header.startswith((b"II*\x00", b"MM\x00*"))
            or (header.startswith(b"RIFF") and header[8:12] == b"WEBP")
            or header.startswith((b"GIF87a", b"GIF89a"))
        ):
            return "image"
        return ""


    def recognize_capture(self, png_bytes: bytes, *, cancel_event=None) -> str:
        """Recognize explicit, in-memory screen pixels without a conversation or path."""
        if not self.config.enabled:
            raise OcrError("ocr_disabled", "OCR 已在设置中关闭。")
        status = self.status()
        if not status.available:
            raise OcrError("ocr_unavailable", status.detail)
        self._check_cancel(cancel_event)
        if not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            raise OcrError("unsupported_type", "截图数据不是 PNG 图片。")
        if self.config.backend == "vision":
            capability, selection = self._vision_selection()
            data = self._vision_image_bytes(BytesIO(png_bytes))
            return asyncio.run(self._vision_text(data, capability, selection, cancel_event))
        image = self._load_image(BytesIO(png_bytes))
        return "\n".join(block.text for block in self._recognize_image(image, cancel_event))

    def _vision_selection(self):
        executor = self._capability_executor
        if executor is None:
            raise OcrError("ocr_unavailable", "视觉 OCR 执行器不可用。")
        capability = executor.get_capability("ocr")
        if not capability.enabled or not capability.model_target.model_ref:
            raise OcrError("ocr_unavailable", "请在 OCR 设置中指定视觉模型。")
        selection = executor.resolve_capability_target(provider=None, capability_id="ocr")
        if selection.source != "explicit" or selection.provider is None:
            raise OcrError("ocr_unavailable", "指定的 OCR 模型不可用，请检查模型与服务设置。")
        profile = selection.provider.effective_model_profile(selection.model)
        if profile.model_type != "chat" or not profile.supports_input("image") or capability.operation != "text" or capability.runtime != "single_turn":
            raise OcrError("ocr_unavailable", "OCR 需要支持图片输入的单轮视觉模型。")
        return capability, selection

    def _vision_image_bytes(self, path: Path | BytesIO) -> bytes:
        image = self._load_pil_image(path)
        try:
            output = BytesIO()
            image.save(output, format="PNG")
            return output.getvalue()
        finally:
            image.close()

    async def _vision_text(self, data: bytes, capability, selection, cancel_event) -> str:
        # Execution configuration is captured once for the whole document.
        self._check_cancel(cancel_event)
        try:
            result = await self._capability_executor.run_capability(
                provider=selection.provider, capability_id="ocr", message="转写这张图片中的文字。",
                config=CapabilitiesConfig(capabilities=(capability,)), resolved_selection=selection,
                images=["data:image/png;base64," + base64.b64encode(data).decode("ascii")],
                context=CapabilityRunContext(caller="ocr", cancel_event=cancel_event))
        except asyncio.CancelledError as exc:
            if cancel_event is None or not cancel_event.is_set():
                raise
            raise OcrError("cancelled", "OCR 已取消。") from exc
        except Exception as exc:
            raise OcrError("inference_failed", f"视觉 OCR 请求失败：{exc}") from exc
        self._check_cancel(cancel_event)
        if result.validation_error:
            raise OcrError("inference_failed", result.validation_error)
        if result.metadata.get("incomplete"):
            raise OcrError("inference_failed", "视觉 OCR 响应未完成，请减少每页内容或增加模型输出额度。")
        return result.content.strip()

    async def _recognize_vision(self, path: Path, *, source_ref: str, source_digest: str,
                                source_name: str, source_mime: str, start_page: int,
                                page_count: int, cancel_event) -> _OcrResult:
        self._check_cancel(cancel_event)
        capability, selection = self._vision_selection()
        started = time.perf_counter()
        resolved = path.expanduser().resolve()
        before = self._stat_signature(resolved)
        digest = await asyncio.to_thread(self._sha256, resolved, cancel_event)
        if source_digest and source_digest != digest:
            raise OcrError("source_changed", "OCR 源文件与输入引用不一致。")
        kind = await asyncio.to_thread(self._classify_source, resolved, source_name=source_name, source_mime=source_mime)
        if kind not in {"pdf", "image"}:
            raise OcrError("unsupported_type", "OCR 仅支持静态图片和 PDF。")
        total = await asyncio.to_thread(self._vision_pdf_page, resolved, None) if kind == "pdf" else 1
        if start_page > total:
            raise OcrError("page_out_of_range", f"start_page 超出范围，共 {total} 页。")
        pages = []
        for number in range(start_page, min(total, start_page + page_count - 1) + 1):
            self._check_cancel(cancel_event)
            if before != self._stat_signature(resolved):
                raise OcrError("source_changed", "文件在 OCR 期间发生变化。")
            data = await asyncio.to_thread(self._vision_pdf_page, resolved, number) if kind == "pdf" else await asyncio.to_thread(self._vision_image_bytes, resolved)
            text = await self._vision_text(data, capability, selection, cancel_event)
            pages.append(_OcrPage(number, (OcrBlock(text),) if text else ()))
        if before != self._stat_signature(resolved) or digest != await asyncio.to_thread(self._sha256, resolved, cancel_event):
            raise OcrError("source_changed", "文件在 OCR 期间发生变化。")
        last = pages[-1].number
        return _OcrResult(source_ref, digest, f"{selection.provider.name}|{selection.model}", total,
                          tuple(page.number for page in pages), tuple(pages),
                          last + 1 if last < total else None, last < total,
                          max(0, round((time.perf_counter() - started) * 1000)))

    @staticmethod
    def _vision_pdf_page(path: Path, number: int | None):
        import pymupdf  # optional PDF renderer, no PP-OCR dependencies
        try:
            with pymupdf.open(str(path)) as document:
                if document.needs_pass:
                    raise OcrError("pdf_encrypted", "PDF 已加密，请先解密。")
                if document.page_count <= 0:
                    raise OcrError("page_out_of_range", "PDF 没有页面。")
                if number is None:
                    return document.page_count
                page = document.load_page(number - 1)
                scale = _IMAGE_MAX_SIDE / max(float(page.rect.width), float(page.rect.height), 1.0)
                return page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csRGB, alpha=False).tobytes("png")
        except OcrError:
            raise
        except Exception as exc:
            raise OcrError("pdf_invalid", "PDF 无法打开或页面渲染失败。") from exc
