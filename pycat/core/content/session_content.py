"""Canonical session input snapshots and request-time materialization."""
from __future__ import annotations

import base64
import codecs
import copy
import hashlib
import io
import json
import mimetypes
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from pycat.core.content.mime import DEFAULT_MIME, guess_mime, is_text_mime
from pycat.core.content.office import extract_office_text, is_office_attachment
from pycat.models.contracts.content import (
    ContentRef,
    InputPreparationFailure,
    InputPreparationResult,
)
from pycat.models.conversation import Conversation, Message
from pycat.models.session_paths import resolve_session_root

MAX_INPUT_FILE_BYTES = 25 * 1024 * 1024
MAX_INPUT_BATCH_BYTES = 64 * 1024 * 1024
MAX_MATERIALIZED_TEXT_BYTES = 1024 * 1024
TEXT_ATTACHMENT_BUDGET_RATIO = 0.25
TEXT_ATTACHMENT_BYTES_PER_TOKEN = 3
_SAFE_CONTENT_ID = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}
_TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".xml",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".log",
    ".py",
    ".pyw",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".css",
    ".html",
    ".htm",
    ".sql",
    ".sh",
    ".bat",
    ".ps1",
}
_OFFICE_EXTENSIONS = {
    ".docx",
    ".docm",
    ".dotx",
    ".dotm",
    ".xlsx",
    ".xlsm",
    ".xltx",
    ".xltm",
}
_GENERIC_EXTENSIONS = {".zip", ".mp3", ".wav", ".mp4", ".mov", ".avi", ".rtf", ".epub"}
_SAFE_STORAGE_NAME = re.compile(r"^original(?:\.[a-z0-9][a-z0-9._-]{0,15})?$")


@dataclass
class RequestContentCache:
    """Verified immutable input data reused only during one LLM request."""

    snapshots: dict[tuple[str, str], tuple[ContentRef, bytes]] = field(default_factory=dict)
    data_urls: dict[tuple[str, str], str] = field(default_factory=dict)


def text_attachment_byte_budget(prompt_limit: int) -> int:
    proportional = int(max(0, int(prompt_limit or 0)) * TEXT_ATTACHMENT_BUDGET_RATIO * TEXT_ATTACHMENT_BYTES_PER_TOKEN)
    return min(MAX_MATERIALIZED_TEXT_BYTES, proportional)


class SessionContentService:
    """Own immutable user-selected inputs below one conversation session root."""

    def __init__(self, *, data_dir: str | None = None, workspace_service=None):
        self.data_dir = data_dir
        self.workspace_service = workspace_service

    def prepare_inputs(
        self,
        conversation: Conversation,
        attachments: Iterable[dict[str, Any]],
        *,
        message_id: str = "",
    ) -> InputPreparationResult:
        prepared: list[ContentRef] = []
        failures: list[InputPreparationFailure] = []
        created_refs: list[ContentRef] = []
        created_ids: set[str] = set()
        total_size = 0
        for attachment in attachments or []:
            if not isinstance(attachment, dict):
                continue
            source = str(attachment.get("path") or "")
            try:
                if isinstance(attachment.get('data'), bytes):
                    raw, detected_name, detected_mime = attachment['data'], 'attachment', ''
                    if len(raw) > MAX_INPUT_FILE_BYTES:
                        raise ValueError(f'attachment exceeds {MAX_INPUT_FILE_BYTES} bytes')
                else:
                    raw, detected_name, detected_mime = self._read_source(source)
                name = self._safe_name(attachment.get("name")) or detected_name
                mime = self._safe_mime(attachment.get("mime")) or detected_mime or guess_mime(name)
                if total_size + len(raw) > MAX_INPUT_BATCH_BYTES:
                    raise ValueError(f"attachment batch exceeds {MAX_INPUT_BATCH_BYTES} bytes")
                ref, created = self._store(
                    conversation,
                    raw=raw,
                    name=name,
                    mime=mime,
                    message_id=message_id,
                )
                if created and ref.id not in created_ids:
                    created_refs.append(ref)
                    created_ids.add(ref.id)
                if self.workspace_service is not None and (files := self.workspace_service.files(conversation.work_dir)):
                    files.write_bytes(self.remote_input_path(conversation, ref), raw)
                total_size += len(raw)
                prepared.append(ref)
            except Exception as exc:
                failures.append(InputPreparationFailure(source=source, error=str(exc)))
        return InputPreparationResult(
            refs=prepared,
            failures=failures,
            created_refs=created_refs,
        )

    @staticmethod
    def remote_input_path(conversation, ref):
        # Identity components come from validated conversation ids and SHA-256 input refs.
        resolve_session_root(conversation.work_dir, conversation.id, data_dir=getattr(conversation, "data_dir", None))
        if not re.fullmatch(r"[a-f0-9]{64}", ref.id):
            raise ValueError("invalid input digest")
        return f".pycat/inputs/{conversation.id}/{ref.id}/{SessionContentService._safe_name(ref.name) or 'input'}"

    def cleanup_unreferenced(
        self,
        conversation: Conversation,
        refs: Iterable[ContentRef | str],
    ) -> list[str]:
        referenced = {
            self._ref_value(ref)
            for message in (getattr(conversation, "messages", []) or [])
            for ref in (getattr(message, "content_refs", []) or [])
        }
        removed: list[str] = []
        for ref in refs or []:
            value = self._ref_value(ref)
            if not value or value in referenced:
                continue
            try:
                record_dir = self._record_dir(conversation, ref)
                if record_dir.is_dir():
                    shutil.rmtree(record_dir)
                removed.append(value)
            except FileNotFoundError:
                removed.append(value)
        return removed

    def load_ref(self, conversation: Conversation, ref: ContentRef | str) -> ContentRef:
        record_dir = self._record_dir(conversation, ref)
        payload = json.loads((record_dir / "meta.json").read_text(encoding="utf-8"))
        loaded = ContentRef.from_dict(payload)
        if (
            loaded.kind != "input"
            or loaded.id != record_dir.name
            or loaded.digest != record_dir.name
            or loaded.ref != f"input:{record_dir.name}"
            or loaded.size < 0
        ):
            raise ValueError("invalid input metadata")
        return loaded

    def resolve_original(self, conversation: Conversation, ref: ContentRef | str) -> Path:
        record_dir = self._record_dir(conversation, ref)
        metadata_path = record_dir / "meta.json"
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise FileNotFoundError(f"input snapshot metadata is missing: {self._ref_value(ref)}")
        storage_name = self._storage_name(payload)
        original = record_dir / storage_name
        if not original.is_file():
            raise FileNotFoundError(f"input snapshot is missing: {self._ref_value(ref)}")
        return original

    def materialize_messages(
        self,
        conversation: Conversation,
        messages: Iterable[Message],
        *,
        include_image_data: bool = True,
        text_byte_budget: int | None = None,
        cache: RequestContentCache | None = None,
    ) -> list[Message]:
        remaining = [
            max(0, int(MAX_MATERIALIZED_TEXT_BYTES if text_byte_budget is None else text_byte_budget))
        ]
        source_messages = list(messages or [])
        newest_first = [
            self._materialize_message(
                conversation,
                message,
                include_image_data=include_image_data,
                remaining_text_bytes=remaining,
                cache=cache,
            )
            for message in reversed(source_messages)
        ]
        return list(reversed(newest_first))

    def materialize_message(
        self,
        conversation: Conversation,
        message: Message,
        *,
        include_image_data: bool = True,
        text_byte_budget: int | None = None,
        cache: RequestContentCache | None = None,
    ) -> Message:
        remaining = [
            max(0, int(MAX_MATERIALIZED_TEXT_BYTES if text_byte_budget is None else text_byte_budget))
        ]
        return self._materialize_message(
            conversation,
            message,
            include_image_data=include_image_data,
            remaining_text_bytes=remaining,
            cache=cache,
        )

    def _materialize_message(
        self,
        conversation: Conversation,
        message: Message,
        *,
        include_image_data: bool,
        remaining_text_bytes: list[int],
        cache: RequestContentCache | None,
    ) -> Message:
        materialized = copy.deepcopy(message)
        refs = list(getattr(message, "content_refs", []) or [])
        if not refs:
            return materialized

        text_parts: list[str] = []
        images: list[str] = list(materialized.images or [])
        for raw_ref in refs:
            try:
                requested = raw_ref if isinstance(raw_ref, ContentRef) else ContentRef.from_dict(raw_ref)
                ref, raw, cache_key = self._load_snapshot(conversation, requested, cache=cache)
                mime = str(ref.mime or DEFAULT_MIME).lower()
                name = str(requested.name or ref.name or "attachment")
                label = f"{name} ({mime}, {ref.ref})"
                if str(conversation.work_dir).startswith("ssh://"):
                    label += f"; remote file: {self.remote_input_path(conversation, ref)}"
                if mime.startswith("image/"):
                    if include_image_data:
                        data_url = cache.data_urls.get(cache_key) if cache is not None else None
                        if data_url is None:
                            encoded = base64.b64encode(raw).decode("ascii")
                            data_url = f"data:{mime};base64,{encoded}"
                            if cache is not None:
                                cache.data_urls[cache_key] = data_url
                        images.append(data_url)
                    else:
                        images.append(ref.ref)
                    text_parts.append(f"[Attachment: {label}]")
                    continue
                if is_office_attachment(name, mime):
                    available = max(0, int(remaining_text_bytes[0] or 0))
                    extracted = extract_office_text(
                        raw,
                        name=name,
                        mime=mime,
                        max_bytes=available,
                    )
                    remaining_text_bytes[0] = max(
                        0,
                        available - len(extracted.text.encode("utf-8")),
                    )
                    suffix = "\n[truncated=true]" if extracted.truncated else ""
                    text_parts.append(
                        f"--- Office attachment: {label} ---\n"
                        f"{extracted.text}{suffix}\n"
                        "--- End Office attachment ---"
                    )
                    continue
                if is_text_mime(ref.mime):
                    available = max(0, int(remaining_text_bytes[0] or 0))
                    included = min(len(raw), available)
                    truncated = included < len(raw)
                    decoder = codecs.getincrementaldecoder("utf-8")()
                    view = decoder.decode(raw[:included], final=not truncated)
                    remaining_text_bytes[0] = max(0, available - included)
                    suffix = "\n[truncated=true]" if truncated else ""
                    text_parts.append(
                        f"--- Attachment: {label} ---\n{view}{suffix}\n--- End Attachment ---"
                    )
                    continue
                text_parts.append(f"[Attachment: {label}; content is not automatically included]")
            except Exception as exc:
                text_parts.append(f"[Attachment unavailable: {self._ref_value(raw_ref)}; {exc}]")

        if text_parts:
            materialized.content = "\n\n".join(
                part for part in (str(materialized.content or ""), *text_parts) if part
            )
        materialized.images = images
        return materialized

    def _store(
        self,
        conversation: Conversation,
        *,
        raw: bytes,
        name: str,
        mime: str,
        message_id: str,
    ) -> tuple[ContentRef, bool]:
        if len(raw) > MAX_INPUT_FILE_BYTES:
            raise ValueError(f"attachment exceeds {MAX_INPUT_FILE_BYTES} bytes: {name}")
        digest = hashlib.sha256(raw).hexdigest()
        input_root = resolve_session_root(conversation.work_dir, conversation.id, data_dir=getattr(conversation, "data_dir", None)) / "input"
        record_dir = input_root / digest
        metadata = record_dir / "meta.json"

        if metadata.exists():
            payload = json.loads(metadata.read_text(encoding="utf-8"))
            existing = ContentRef.from_dict(payload)
            if existing.digest == digest and existing.size == len(raw):
                original = record_dir / self._storage_name(payload)
                existing_raw = self._read_bounded_file(original)
                if len(existing_raw) != len(raw) or hashlib.sha256(existing_raw).hexdigest() != digest:
                    raise ValueError(f"existing input snapshot failed integrity validation: {digest}")
                return ContentRef(
                    **{
                        **existing.to_dict(),
                        "name": name or existing.name,
                        "mime": mime or existing.mime,
                        "message_id": str(message_id or ""),
                    }
                ), False
            raise ValueError(f"input metadata does not match content digest: {digest}")

        ref = ContentRef(
            id=digest,
            name=name or "attachment",
            mime=mime or DEFAULT_MIME,
            size=len(raw),
            digest=digest,
            ref=f"input:{digest}",
            message_id="",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        storage_name = self._storage_name_for_bytes(raw, name=name, mime=mime)
        created = self._publish_record(
            input_root,
            record_dir,
            raw=raw,
            ref=ref,
            storage_name=storage_name,
        )
        stored_payload = json.loads(metadata.read_text(encoding="utf-8"))
        stored = ContentRef.from_dict(stored_payload)
        original = record_dir / self._storage_name(stored_payload)
        stored_raw = self._read_bounded_file(original)
        if len(stored_raw) != stored.size or hashlib.sha256(stored_raw).hexdigest() != stored.digest:
            raise ValueError(f"input snapshot failed integrity validation: {digest}")
        return ContentRef(
            **{
                **stored.to_dict(),
                "name": name or stored.name,
                "mime": mime or stored.mime,
                "message_id": str(message_id or ""),
            }
        ), created

    @staticmethod
    def _publish_record(
        input_root: Path,
        record_dir: Path,
        *,
        raw: bytes,
        ref: ContentRef,
        storage_name: str,
    ) -> bool:
        storage_name = SessionContentService._validate_storage_name(storage_name)
        input_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{record_dir.name}-", dir=input_root))
        created = False
        try:
            (staging / storage_name).write_bytes(raw)
            metadata = ref.to_dict()
            metadata["storage_name"] = storage_name
            (staging / "meta.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            try:
                os.rename(staging, record_dir)
                created = True
            except OSError:
                if not record_dir.is_dir():
                    raise
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        return created

    @staticmethod
    def _storage_name(payload: dict[str, Any] | None) -> str:
        """Return a safe physical blob name, accepting legacy ``original`` records."""

        raw = str((payload or {}).get("storage_name") or "original").strip()
        return SessionContentService._validate_storage_name(raw)

    @staticmethod
    def _validate_storage_name(value: str) -> str:
        name = str(value or "").strip()
        if not _SAFE_STORAGE_NAME.fullmatch(name) or Path(name).name != name:
            raise ValueError("invalid input storage name")
        return name

    @classmethod
    def _storage_name_for_bytes(cls, raw: bytes, *, name: str, mime: str) -> str:
        """Choose a small interoperable suffix without treating it as type authority."""

        extension = cls._detect_storage_extension(raw, name=name, mime=mime)
        return f"original{extension}" if extension else "original.bin"

    @classmethod
    def _detect_storage_extension(cls, raw: bytes, *, name: str, mime: str) -> str:
        # Signatures take precedence over a misleading source name or MIME.
        if raw.startswith(b"%PDF-"):
            return ".pdf"
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if raw.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if raw[:6] in {b"GIF87a", b"GIF89a"}:
            return ".gif"
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            return ".webp"
        if raw.startswith(b"BM"):
            return ".bmp"
        if raw[:4] in {b"II*\x00", b"MM\x00*"}:
            return ".tiff"

        source_suffix = Path(str(name or "")).suffix.lower()
        normalized_mime = str(mime or "").split(";", 1)[0].strip().lower()
        if raw[:4] == b"PK\x03\x04":
            package_extension = cls._office_package_extension(raw, source_suffix)
            if package_extension:
                return package_extension
            if source_suffix == ".zip" or normalized_mime == "application/zip":
                return ".zip"

        if normalized_mime == "application/pdf":
            return ".pdf"
        image_extension = _IMAGE_EXTENSIONS.get(normalized_mime)
        if image_extension:
            return image_extension

        if cls._looks_like_utf8_text(raw):
            if source_suffix in _TEXT_EXTENSIONS:
                return source_suffix
            guessed = mimetypes.guess_extension(normalized_mime) if normalized_mime else None
            if guessed and guessed.lower() in _TEXT_EXTENSIONS:
                return guessed.lower()
            return ".txt"

        if source_suffix in _OFFICE_EXTENSIONS | _GENERIC_EXTENSIONS:
            return source_suffix
        return ".bin"

    @staticmethod
    def _looks_like_utf8_text(raw: bytes) -> bool:
        if not raw or b"\x00" in raw:
            return False
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return not any(ord(character) < 9 or 13 < ord(character) < 32 for character in text)

    @staticmethod
    def _office_package_extension(raw: bytes, source_suffix: str) -> str:
        """Identify an OOXML package from its ZIP members/content types."""

        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as package:
                names = {str(item).lower() for item in package.namelist()}
                if "word/document.xml" in names:
                    if source_suffix in {".docx", ".docm", ".dotx", ".dotm"}:
                        return source_suffix
                    return ".docx"
                if "xl/workbook.xml" in names:
                    if source_suffix in {".xlsx", ".xlsm", ".xltx", ".xltm"}:
                        return source_suffix
                    return ".xlsx"
                if any(item.startswith("word/") for item in names):
                    return ".docx"
                if any(item.startswith("xl/") for item in names):
                    return ".xlsx"
        except (OSError, zipfile.BadZipFile):
            return ""
        return ""

    @staticmethod
    def _read_source(source: str) -> tuple[bytes, str, str]:
        if source.startswith("data:"):
            header, separator, payload = source.partition(",")
            if not separator or ";base64" not in header.lower():
                raise ValueError("only base64 data URLs are supported")
            mime = header[5:].split(";", 1)[0].strip().lower() or DEFAULT_MIME
            if not mime.startswith("image/"):
                raise ValueError("only image data URLs are supported")
            max_encoded_size = ((MAX_INPUT_FILE_BYTES + 2) // 3) * 4
            if len(payload) > max_encoded_size:
                raise ValueError(f"attachment exceeds {MAX_INPUT_FILE_BYTES} bytes")
            try:
                raw = base64.b64decode(payload, validate=True)
            except Exception as exc:
                raise ValueError("invalid base64 attachment") from exc
            extension = _IMAGE_EXTENSIONS.get(mime, mimetypes.guess_extension(mime) or "")
            return raw, f"pasted-image{extension}", mime

        path = Path(source).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError(f"attachment is not a file: {source}")
        with path.open("rb") as stream:
            raw = stream.read(MAX_INPUT_FILE_BYTES + 1)
        if len(raw) > MAX_INPUT_FILE_BYTES:
            raise ValueError(f"attachment exceeds {MAX_INPUT_FILE_BYTES} bytes: {path.name}")
        mime = guess_mime(path.name)
        return raw, path.name, mime

    @staticmethod
    def _safe_name(value: Any) -> str:
        name = re.split(r"[\\/]", str(value or "").strip())[-1]
        name = "".join(character for character in name if ord(character) >= 32).strip()
        return name[:255] if name not in {".", ".."} else ""

    @staticmethod
    def _safe_mime(value: Any) -> str:
        mime = str(value or "").strip().lower()
        if not mime or len(mime) > 127 or "/" not in mime:
            return ""
        if any(ord(character) < 33 or ord(character) > 126 for character in mime):
            return ""
        return mime

    def _load_snapshot(
        self,
        conversation: Conversation,
        requested: ContentRef,
        *,
        cache: RequestContentCache | None,
    ) -> tuple[ContentRef, bytes, tuple[str, str]]:
        record_dir = self._record_dir(conversation, requested)
        cache_key = (str(record_dir.parent.resolve()), record_dir.name)
        if cache is not None and cache_key in cache.snapshots:
            ref, raw = cache.snapshots[cache_key]
            return ref, raw, cache_key

        ref = self.load_ref(conversation, requested)
        original = self.resolve_original(conversation, ref)
        raw = self._read_bounded_file(original)
        if (
            len(raw) > MAX_INPUT_FILE_BYTES
            or len(raw) != ref.size
            or hashlib.sha256(raw).hexdigest() != ref.digest
        ):
            raise ValueError("input snapshot failed integrity validation")
        if cache is not None:
            cache.snapshots[cache_key] = (ref, raw)
        return ref, raw, cache_key

    @staticmethod
    def _read_bounded_file(path: Path) -> bytes:
        with path.open("rb") as stream:
            return stream.read(MAX_INPUT_FILE_BYTES + 1)

    @classmethod
    def _record_dir(cls, conversation: Conversation, ref: ContentRef | str) -> Path:
        value = cls._ref_value(ref)
        prefix, separator, content_id = value.partition(":")
        if prefix != "input" or not separator or not _SAFE_CONTENT_ID.fullmatch(content_id):
            raise ValueError(f"unsafe input ref: {value!r}")
        input_root = resolve_session_root(conversation.work_dir, conversation.id, data_dir=getattr(conversation, "data_dir", None)) / "input"
        record_dir = input_root / content_id
        if record_dir.parent != input_root:
            raise ValueError(f"unsafe input ref: {value!r}")
        return record_dir

    @staticmethod
    def _ref_value(ref: ContentRef | str | Any) -> str:
        if isinstance(ref, ContentRef):
            return str(ref.ref or "")
        if isinstance(ref, dict):
            return str(ref.get("ref") or "")
        return str(ref or "")
