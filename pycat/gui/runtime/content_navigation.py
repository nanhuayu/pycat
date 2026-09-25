"""Qt-side actions for content already verified by the core resolver."""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import shutil
import tempfile
from copy import copy
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Any, Callable, Protocol

from PyQt6.QtCore import QBuffer, QIODevice, QMimeData, QUrl
from PyQt6.QtGui import QDesktopServices, QGuiApplication, QImage, QImageReader, QPixmap

from pycat.core.content.export import render_document
from pycat.core.content.mime import DEFAULT_MIME, guess_mime, is_text_mime
from pycat.core.content.office import OfficeExtractionError, extract_office_text, is_office_attachment
from pycat.core.content.references import (
    build_artifact_content_ref,
    content_identity,
    delivery_refs_for_messages,
    material_rows,
)
from pycat.core.content.resolver import ResolvedContent, SessionContentResolver
from pycat.core.state.artifact import ArtifactService
from pycat.gui.utils.image_loader import read_image
from pycat.models.contracts.content import ContentRef
from pycat.models.session_paths import has_active_workspace
from pycat.models.workspace import WorkspaceLocation, workspace_identity

logger = logging.getLogger(__name__)
_MODERN_OFFICE_SUFFIXES = {".docx", ".docm", ".dotx", ".dotm", ".xlsx", ".xlsm", ".xltx", ".xltm"}


class ContentNavigationError(ValueError):
    """A user-visible content resolution or action failure."""


@dataclass(frozen=True)
class ContentOpenTarget:
    """A local target, or remote metadata with a worker-only materializer."""

    path: Path | None
    ref: ContentRef
    mime: str
    is_image: bool
    preview_kind: str
    actions: tuple[str, ...]
    conversation_id: str = ""
    conversation_workspace: str = ""
    materialize: Callable[[], ContentOpenTarget] | None = field(default=None, repr=False, compare=False)

    @property
    def name(self) -> str:
        return str(self.ref.name or (self.path.name if self.path else "content"))


@dataclass(frozen=True)
class ClipboardPayload:
    """Worker-safe clipboard payload; Qt clipboard objects are created later."""

    path: Path
    image_bytes: bytes | None = None


@dataclass(frozen=True)
class PreparedSave:
    """A verified temporary copy waiting for a GUI-thread commit."""

    destination: Path
    temporary: Path


class ContentTargetResolver(Protocol):
    """Capture scope on the GUI thread; False must not download remote content."""

    def __call__(
        self,
        ref: ContentRef,
        *,
        verify_integrity: bool,
    ) -> ContentOpenTarget: ...


@dataclass(frozen=True)
class TextPreview:
    text: str
    total_bytes: int
    truncated: bool


@dataclass(frozen=True)
class PreparedPreview:
    kind: str
    text: str = ""
    image: QImage | None = None
    page: int = 1
    pages: int = 0
    notice: str = ""


class ContentOpenUseCase:
    """Resolve current-conversation refs and expose stateless desktop actions."""

    def __init__(self, content_service: Any) -> None:
        self._resolver = SessionContentResolver(content_service)

    @staticmethod
    def prepare_preview(target: ContentOpenTarget, *, page: int = 1) -> PreparedPreview:
        """Read/convert off the GUI thread; widgets only apply this bounded result."""
        ContentOpenUseCase.verify_target(target)
        if target.is_image:
            return PreparedPreview("image", image=ContentOpenUseCase.read_image(str(target.path)))
        if target.preview_kind == "pdf":
            pages = ContentOpenUseCase.pdf_page_count(target)
            number = min(max(1, page), max(1, pages))
            return PreparedPreview("pdf", image=ContentOpenUseCase.render_pdf_page(target, number),
                                   page=number, pages=pages)
        if target.preview_kind in {"text", "office"}:
            preview = ContentOpenUseCase.text_preview(target)
            markdown = target.mime == "text/markdown" or target.path.suffix.lower() in {".md", ".markdown"}
            notice = f"仅显示前 256 KiB；原文件共 {preview.total_bytes} bytes。" if preview.truncated else ""
            if target.mime in {"text/html", "application/xhtml+xml"} or target.path.suffix.lower() in {".html", ".htm", ".xhtml"}:
                notice = "HTML 源码 · 可在外部浏览器中查看页面。" + notice
            return PreparedPreview("markdown" if markdown else "text", text=preview.text, notice=notice)
        return PreparedPreview("file", text=target.name + "\n" + str(target.path))

    @staticmethod
    def image_bytes(source: str) -> bytes:
        if source.startswith("data:"):
            payload = source.partition(",")[2]
            payload += "=" * ((-len(payload)) % 4)
            return base64.b64decode(payload, validate=True)
        return Path(source).read_bytes()

    @staticmethod
    def read_image(source: str) -> QImage:
        try:
            return read_image(source)
        except (ValueError, OSError) as exc:
            raise ContentNavigationError(str(exc)) from exc

    def resolve_path(self, conversation: Any, path: str | Path) -> ContentOpenTarget:
        """Preserve a registered Artifact's identity when opened from a file shortcut."""
        candidate = Path(path).resolve()
        for artifact in conversation.get_state().artifacts.values():
            if artifact.content_path and ArtifactService.resolve_content_path(
                    artifact.content_path, work_dir=conversation.work_dir, data_dir=getattr(conversation, "data_dir", None)).resolve() == candidate:
                return self.resolve(conversation, build_artifact_content_ref(
                    artifact, work_dir=conversation.work_dir, conversation_id=conversation.id))
        return self.classify_local_file(candidate)

    def resolve_workspace_file(self, conversation: Any, path: str) -> ContentOpenTarget:
        """Open an explicit user-selected project path through its bounded resolver."""
        requested = ContentRef(id=path, name=path.rsplit("/", 1)[-1], kind="workspace",
            ref=f"workspace:{path}", workspace=conversation.work_dir, mime="", size=0, digest="")
        return self._resolve_target(conversation, requested, verify_integrity=True)

    def resolve(
        self,
        conversation: Any,
        ref: ContentRef | dict[str, Any] | str,
        *,
        verify_integrity: bool = True,
        evidence: bool = False,
    ) -> ContentOpenTarget:
        requested = self._coerce_ref(ref)
        explicit_evidence = evidence and len(requested.digest) == 64 and (
            requested.kind in {"archive", "artifact"} and bool(requested.conversation_id)
            or requested.kind == "wiki" and bool(requested.workspace))
        if not explicit_evidence and not self._belongs_to_conversation(conversation, requested):
            raise ContentNavigationError("该内容不属于当前会话")
        return self._resolve_target(conversation, requested, verify_integrity=verify_integrity)

    def _resolve_target(self, conversation, requested, *, verify_integrity):
        workspace = requested.workspace or conversation.work_dir
        if not verify_integrity and requested.kind == "workspace" and WorkspaceLocation.parse(workspace).is_remote:
            # Membership was checked against the live GUI projection. Capture
            # its scope now; the worker must never consult mutable Qt state.
            snapshot = copy(conversation)
            requested = replace(requested, workspace=workspace)
            mime = self._display_mime(requested.name, requested.mime)
            is_image = mime.startswith("image/")
            kind = ""
            if is_image:
                kind = "image"
            elif mime == "application/pdf":
                kind = "pdf"
            elif is_text_mime(mime):
                kind = "text"
            elif Path(requested.name).suffix.lower() in _MODERN_OFFICE_SUFFIXES:
                kind = "office"
            actions = ("open", "save_as", "reveal", "copy")
            if kind:
                actions = ("preview", *actions)
            return ContentOpenTarget(None, requested, mime, is_image, kind,
                actions,
                conversation_id=str(conversation.id),
                conversation_workspace=str(conversation.work_dir),
                materialize=partial(self._resolve_target, snapshot, requested, verify_integrity=True))
        try:
            resolved = self._resolver.resolve_content(conversation, requested, verify_digest=verify_integrity)
            self._verify_integrity(resolved, verify_digest=verify_integrity and requested.kind == "input")
            mime, is_image, preview_kind = self._verified_type(resolved)
        except ContentNavigationError:
            raise
        except Exception as exc:
            raise ContentNavigationError(self._friendly_error(exc)) from exc
        actions = ("open", "save_as", "reveal", "copy")
        if preview_kind:
            actions = ("preview", *actions)
        return ContentOpenTarget(
            path=resolved.path,
            ref=resolved.ref,
            mime=mime,
            is_image=is_image,
            preview_kind=preview_kind,
            actions=actions,
            conversation_id=str(getattr(conversation, "id", "") or ""),
            conversation_workspace=str(getattr(conversation, "work_dir", "") or ""),
        )

    @staticmethod
    def prepare_target(target: ContentOpenTarget) -> ContentOpenTarget:
        """Materialize and verify in a worker, returning an actual local target."""
        if target.materialize is not None:
            return target.materialize()
        ContentOpenUseCase.verify_target(target)
        return target

    @staticmethod
    def verify_target(target: ContentOpenTarget) -> None:
        """Verify an immutable target without consulting GUI or conversation state."""

        if target.path is None:
            raise ContentNavigationError("远程内容尚未加载")
        resolved = ResolvedContent(path=target.path, ref=target.ref)
        try:
            if not resolved.path.is_file():
                raise ContentNavigationError("内容文件不存在或不可访问")
            ContentOpenUseCase._verify_integrity(resolved, verify_digest=True)
        except ContentNavigationError:
            raise
        except Exception as exc:
            raise ContentNavigationError(
                ContentOpenUseCase._friendly_error(exc)
            ) from exc


    @staticmethod
    def text_preview(target: ContentOpenTarget, *, max_bytes: int = 256 * 1024) -> TextPreview:
        limit = max(1, int(max_bytes or 0))
        if target.preview_kind == "text":
            total = int(target.path.stat().st_size)
            with target.path.open("rb") as stream:
                raw = stream.read(limit + 1)
            truncated = total > limit
            visible = raw[:limit]
            try:
                text = visible.decode("utf-8")
            except UnicodeDecodeError as exc:
                if not truncated:
                    raise ContentNavigationError("文本不是有效的 UTF-8 编码") from exc
                text = visible.decode("utf-8", errors="ignore")
            return TextPreview(text=text, total_bytes=total, truncated=truncated)
        if target.preview_kind == "office":
            try:
                extracted = extract_office_text(
                    target.path,
                    name=target.name,
                    mime=target.mime,
                    max_bytes=limit,
                )
            except OfficeExtractionError as exc:
                raise ContentNavigationError(f"Office 预览失败：{exc}") from exc
            return TextPreview(
                text=extracted.text,
                total_bytes=int(target.path.stat().st_size),
                truncated=bool(extracted.truncated),
            )
        raise ContentNavigationError("该内容不支持文本预览")

    @staticmethod
    def pdf_page_count(target: ContentOpenTarget) -> int:
        import pymupdf

        if target.preview_kind != "pdf":
            raise ContentNavigationError("该内容不是 PDF")
        try:
            document = pymupdf.open(str(target.path))
        except Exception as exc:
            raise ContentNavigationError("PDF 无法打开或文件已经损坏") from exc
        try:
            if bool(getattr(document, "needs_pass", False)):
                raise ContentNavigationError("PDF 已加密，无法预览")
            return int(document.page_count)
        finally:
            document.close()

    @staticmethod
    def render_pdf_page(
        target: ContentOpenTarget,
        page_number: int,
        *,
        max_side: int = 1600,
    ) -> QImage:
        import pymupdf

        if target.preview_kind != "pdf":
            raise ContentNavigationError("该内容不是 PDF")
        try:
            document = pymupdf.open(str(target.path))
        except Exception as exc:
            raise ContentNavigationError("PDF 无法打开或文件已经损坏") from exc
        try:
            if bool(getattr(document, "needs_pass", False)):
                raise ContentNavigationError("PDF 已加密，无法预览")
            total = int(document.page_count)
            number = int(page_number or 0)
            if number < 1 or number > total:
                raise ContentNavigationError(f"PDF 页码超出范围，共 {total} 页")
            page = document.load_page(number - 1)
            longest = max(float(page.rect.width), float(page.rect.height), 1.0)
            scale = min(2.0, max(0.1, float(max(1, int(max_side))) / longest))
            pixmap = page.get_pixmap(
                matrix=pymupdf.Matrix(scale, scale),
                colorspace=pymupdf.csRGB,
                alpha=False,
            )
            image = QImage(
                pixmap.samples,
                pixmap.width,
                pixmap.height,
                pixmap.stride,
                QImage.Format.Format_RGB888,
            )
            return image.copy()
        except ContentNavigationError:
            raise
        except Exception as exc:
            raise ContentNavigationError("PDF 页面无法渲染") from exc
        finally:
            document.close()

    @staticmethod
    def save_as(target: ContentOpenTarget, destination: str | Path) -> Path:
        prepared = ContentOpenUseCase.prepare_save_as(target, destination)
        try:
            return ContentOpenUseCase.commit_save_as(prepared)
        except Exception as exc:
            ContentOpenUseCase.discard_save_as(prepared)
            if isinstance(exc, ContentNavigationError):
                raise
            raise ContentNavigationError(f"另存失败：{exc}") from exc

    @staticmethod
    def prepare_save_as(target: ContentOpenTarget, destination: str | Path, *, format: str | None = None) -> PreparedSave:
        """Copy to a temporary file and verify it before any visible commit."""

        output = Path(destination).expanduser()
        if not str(output):
            raise ContentNavigationError("未选择保存位置")
        temporary: Path | None = None
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{output.name}.pycat-",
                dir=str(output.parent),
            )
            os.close(fd)
            temporary = Path(temporary_name)
            shutil.copyfile(target.path, temporary)
            resolved = ResolvedContent(path=temporary, ref=target.ref)
            ContentOpenUseCase._verify_integrity(resolved, verify_digest=True)
            if format:
                if target.path.suffix.lower() not in {".md", ".markdown"}:
                    raise ContentNavigationError("仅 Markdown 文件支持文档格式转换")
                text = temporary.read_text(encoding="utf-8-sig")
                temporary.write_bytes(render_document(text, format, title=target.path.stem,
                                                     source_dir=target.path.parent))
            return PreparedSave(destination=output, temporary=temporary)
        except ContentNavigationError:
            if temporary is not None:
                ContentOpenUseCase._unlink_quietly(temporary)
            raise
        except Exception as exc:
            if temporary is not None:
                ContentOpenUseCase._unlink_quietly(temporary)
            raise ContentNavigationError(f"另存准备失败：{exc}") from exc

    @staticmethod
    def commit_save_as(prepared: PreparedSave) -> Path:
        """Commit a prepared file; this small rename is intended for GUI thread use."""

        try:
            os.replace(prepared.temporary, prepared.destination)
        except Exception as exc:
            ContentOpenUseCase._unlink_quietly(prepared.temporary)
            raise ContentNavigationError(f"另存失败：{exc}") from exc
        return prepared.destination

    @staticmethod
    def discard_save_as(prepared: PreparedSave | object) -> None:
        if isinstance(prepared, PreparedSave):
            ContentOpenUseCase._unlink_quietly(prepared.temporary)

    @staticmethod
    def classify_local_file(path: str | Path) -> ContentOpenTarget:
        """Classify a standalone local file for preview-first opening.

        Builds a synthetic input ref (no session/integrity checks — the file
        typically comes straight from a tool write) and reuses the shared
        signature sniffing so previews and system opens share one rule set.
        """

        file_path = Path(path).expanduser()
        if not file_path.is_file():
            raise ContentNavigationError("文件不存在或已经被删除")
        ref = ContentRef(
            id=file_path.name,
            name=file_path.name,
            mime="",
            size=0,
            digest="",
            ref=str(file_path),
            kind="input",
        )
        resolved = ResolvedContent(path=file_path, ref=ref)
        mime, is_image, preview_kind = ContentOpenUseCase._verified_type(resolved)
        actions: tuple[str, ...] = ("open", "reveal")
        if preview_kind:
            actions = ("preview", *actions)
        return ContentOpenTarget(
            path=file_path,
            ref=ref,
            mime=mime,
            is_image=is_image,
            preview_kind=preview_kind,
            actions=actions,
        )

    @staticmethod
    def open_system(target: ContentOpenTarget) -> bool:
        return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(target.path))))

    @staticmethod
    def reveal(target: ContentOpenTarget) -> bool:
        return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(target.path.parent))))

    @staticmethod
    def copy_to_clipboard(target: ContentOpenTarget) -> None:
        payload = ContentOpenUseCase.prepare_clipboard(target)
        ContentOpenUseCase.write_clipboard(payload)

    @staticmethod
    def prepare_clipboard(target: ContentOpenTarget) -> ClipboardPayload:
        """Prepare clipboard data without creating or touching Qt GUI objects."""

        if not target.is_image:
            ContentOpenUseCase.verify_target(target)
            return ClipboardPayload(path=Path(target.path))

        try:
            data = Path(target.path).read_bytes()
        except Exception as exc:
            raise ContentNavigationError(f"图片读取失败：{exc}") from exc
        ContentOpenUseCase._verify_bytes(target.ref, data)
        if target.mime == 'image/svg+xml':
            source = 'data:image/svg+xml;base64,' + base64.b64encode(data).decode('ascii')
            image = ContentOpenUseCase.read_image(source)
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            image.save(buffer, 'PNG')
            data = bytes(buffer.data())
        return ClipboardPayload(path=Path(target.path), image_bytes=data)

    @staticmethod
    def write_clipboard(payload: ClipboardPayload) -> None:
        """Write a prepared payload to the desktop clipboard on the GUI thread."""

        clipboard = QGuiApplication.clipboard()
        if payload.image_bytes is not None:
            pixmap = QPixmap()
            pixmap.loadFromData(payload.image_bytes)
            if pixmap.isNull():
                raise ContentNavigationError("图片格式无效或文件已经损坏")
            clipboard.setPixmap(pixmap)
            return
        mime_data = QMimeData()
        mime_data.setUrls([QUrl.fromLocalFile(str(payload.path))])
        clipboard.setMimeData(mime_data)

    @staticmethod
    def _coerce_ref(ref: ContentRef | dict[str, Any] | str) -> ContentRef:
        if isinstance(ref, ContentRef):
            return ref
        if isinstance(ref, dict):
            return ContentRef.from_dict(ref)
        value = str(ref or "").strip()
        kind, separator, identifier = value.partition(":")
        return ContentRef(
            id=identifier if separator else value,
            name=identifier if separator else value,
            mime=DEFAULT_MIME,
            size=0,
            digest="",
            ref=value,
            kind=kind if separator else "input",
        )

    @classmethod
    def _belongs_to_conversation(cls, conversation: Any, requested: ContentRef) -> bool:
        if requested.kind == "wiki":
            return has_active_workspace(conversation.work_dir) and (
                workspace_identity(requested.workspace or conversation.work_dir)
                == workspace_identity(conversation.work_dir))
        key = (str(requested.kind or "input"), str(requested.ref or ""))
        if not key[1]:
            return False
        known: set[tuple[str, str]] = set()
        for message in getattr(conversation, "messages", []) or []:
            for content_ref in getattr(message, "content_refs", []) or []:
                known.add((str(content_ref.kind or "input"), str(content_ref.ref or "")))
        for content_ref in delivery_refs_for_messages(getattr(conversation, "messages", []) or []):
            known.add((str(content_ref.kind or ""), str(content_ref.ref or "")))
        try:
            for name in (conversation.get_state().artifacts or {}):
                known.add(("artifact", f"artifact:{name}"))
        except Exception:
            pass
        if requested.kind == "archive":
            for message in getattr(conversation, "messages", []) or []:
                for tool_call in getattr(message, "tool_calls", []) or []:
                    if not isinstance(tool_call, dict):
                        continue
                    result = tool_call.get("result") if isinstance(tool_call.get("result"), dict) else {}
                    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
                    content_id = str(metadata.get("content_id") or "").strip()
                    if content_id:
                        known.add(("archive", f"archive:{content_id}"))
        if key in known:
            # A pinned foreign owner must itself occur in the conversation.
            if requested.conversation_id and requested.conversation_id != conversation.id or (
                requested.workspace and workspace_identity(requested.workspace) !=
                workspace_identity(conversation.work_dir)):
                wanted = content_identity(requested)
                refs = [ref for message in conversation.messages for ref in message.content_refs or []]
                refs += delivery_refs_for_messages(conversation.messages)
                return any(content_identity(ref, work_dir=conversation.work_dir, conversation_id=conversation.id) == wanted for ref in refs)
            return True
        if requested.kind == "workspace":
            return any(row["ref"] and content_identity(ContentRef.from_dict(row["ref"])) == content_identity(
                requested, work_dir=conversation.work_dir, conversation_id=conversation.id)
                for row in material_rows(conversation) if row.get("change"))
        return False

    @staticmethod
    def _verify_integrity(
        resolved: ResolvedContent,
        *,
        verify_digest: bool = True,
    ) -> None:
        ref = resolved.ref
        kind = str(ref.kind or "input").strip().lower()
        expected_size = int(ref.size or 0)
        expected_digest = str(ref.digest or "").strip().lower()
        if kind in {"input", "workspace"} or (kind == "archive" and "/images/" in ref.id):
            stat = resolved.path.stat()
            if expected_size > 0 and int(stat.st_size) != expected_size:
                raise ContentNavigationError("文件大小与会话记录不一致")
            if verify_digest and len(expected_digest) == 64:
                digest = hashlib.sha256()
                with resolved.path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != expected_digest:
                    raise ContentNavigationError("文件内容与会话记录不一致")
            return
        if kind == "artifact" and verify_digest:
            try:
                text = resolved.path.read_text(encoding="utf-8")
            except UnicodeError as exc:
                raise ContentNavigationError("Artifact 不是有效的 UTF-8 文本") from exc
            if expected_size > 0 and len(text) != expected_size:
                raise ContentNavigationError("Artifact 字符数与会话记录不一致")
            if (
                len(expected_digest) == 64
                and hashlib.sha256(text.encode("utf-8")).hexdigest() != expected_digest
            ):
                raise ContentNavigationError("Artifact 内容与会话记录不一致")
            return
        # Archive digests may cover the original text plus image sidecars. The
        # archive owner has already validated the record and path containment;
        # hashing only ``original`` here would compare different identities.

    @staticmethod
    def _verify_bytes(ref: ContentRef, data: bytes) -> None:
        kind = str(ref.kind or "input").strip().lower()
        expected_size = int(ref.size or 0)
        expected_digest = str(ref.digest or "").strip().lower()
        if kind in {"input", "workspace"} or (kind == "archive" and "/images/" in ref.id):
            if expected_size > 0 and len(data) != expected_size:
                raise ContentNavigationError("文件大小与会话记录不一致")
            if len(expected_digest) == 64 and hashlib.sha256(data).hexdigest() != expected_digest:
                raise ContentNavigationError("文件内容与会话记录不一致")
            return
        if kind == "artifact":
            try:
                text = data.decode("utf-8")
            except UnicodeError as exc:
                raise ContentNavigationError("Artifact 不是有效的 UTF-8 文本") from exc
            if expected_size > 0 and len(text) != expected_size:
                raise ContentNavigationError("Artifact 字符数与会话记录不一致")
            if len(expected_digest) == 64 and hashlib.sha256(text.encode("utf-8")).hexdigest() != expected_digest:
                raise ContentNavigationError("Artifact 内容与会话记录不一致")

    @staticmethod
    def _unlink_quietly(path: Path) -> None:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _display_mime(name: str, declared: str) -> str:
        declared = str(declared or "").lower()
        guessed = guess_mime(name)
        # Senders may label passive text by the OS handler (CSV as Excel).
        if not declared or declared == DEFAULT_MIME or is_text_mime(guessed) and not is_text_mime(declared):
            return guessed
        return declared

    @staticmethod
    def _verified_type(resolved: ResolvedContent) -> tuple[str, bool, str]:
        declared = str(resolved.mime or DEFAULT_MIME).lower()
        suffix_mime = guess_mime(resolved.name)
        mime = ContentOpenUseCase._display_mime(resolved.name, declared)
        suffix = Path(resolved.name).suffix.lower()
        with resolved.path.open("rb") as stream:
            signature = stream.read(8)
        claims_pdf = mime in {"application/pdf", "application/x-pdf"} or resolved.name.lower().endswith(".pdf")
        if claims_pdf:
            if not signature.startswith(b"%PDF-"):
                raise ContentNavigationError("PDF 格式无效或文件已经损坏")
            return "application/pdf", False, "pdf"
        if is_office_attachment(resolved.name, mime):
            if suffix in _MODERN_OFFICE_SUFFIXES:
                if not signature.startswith(b"PK\x03\x04"):
                    raise ContentNavigationError("Office 格式无效或文件已经损坏")
                return mime, False, "office"
            return mime, False, ""

        # HTML is readable source only; never send active markup to Markdown
        # or an embedded browser. SVG uses a passive native Qt rendering copy.
        if mime in {"text/html", "application/xhtml+xml"} or suffix in {".htm", ".html", ".xhtml"}:
            return mime, False, "text"
        if mime == "image/svg+xml" or suffix in {".svg", ".svgz"}:
            return "image/svg+xml", True, "image"

        claims_image = declared.startswith("image/") or suffix_mime.startswith("image/")
        reader = QImageReader(str(resolved.path))
        image_format = bytes(reader.format()).decode("ascii", errors="ignore").lower()
        raster_formats = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff"}
        if image_format not in raster_formats:
            image_format = ""
        if claims_image and (not image_format or not reader.canRead()):
            raise ContentNavigationError("图片格式无效或文件已经损坏")
        if not image_format:
            if is_text_mime(mime):
                return mime, False, "text"
            return mime, False, ""
        detected = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "tif": "image/tiff",
            "tiff": "image/tiff",
        }.get(image_format, f"image/{image_format}")
        if declared.startswith("image/") and declared != detected:
            aliases = {declared, detected}
            if aliases != {"image/jpg", "image/jpeg"}:
                raise ContentNavigationError("图片声明格式与文件签名不一致")
        return detected, True, "image"

    @staticmethod
    def _friendly_error(error: Exception) -> str:
        if isinstance(error, FileNotFoundError):
            return "文件不存在或已经被删除"
        message = str(error or "").strip()
        return message or "内容无法打开"
