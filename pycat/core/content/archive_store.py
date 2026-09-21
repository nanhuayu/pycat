"""Session archive storage for recoverable context content.

Archived content keeps the original bytes/text as the source of truth. Summary
and other compressed views are derived data written beside the original and can
always be regenerated.
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from pycat.models.contracts.content import (
    ARCHIVE_KINDS,
    ArchivedContentRecord,
    ArchivedContentView,
    ContentRef,
    normalize_archive_kind,
)
from pycat.models.session_paths import resolve_project_data_root, normalize_work_dir, resolve_session_root
from pycat.core.persistence import atomic_write_text, atomic_write_bytes

ARCHIVE_KIND_DIRS: dict[str, str] = {
    "tool_call": "tool-call",
    "history": "history",
}


def estimate_tokens(text: str) -> int:
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", str(text or "")))
    other_chars = len(str(text or "")) - chinese_chars
    return int(chinese_chars + other_chars * 0.25)


class SessionArchiveStore:
    """Read/write recoverable content under one conversation session."""

    def __init__(self, work_dir: str, conversation_id: object = None, *, data_dir: str | Path | None = None):
        raw_work_dir = normalize_work_dir(work_dir)
        self.workspace = raw_work_dir
        self.work_dir = (resolve_project_data_root(raw_work_dir, data_dir=data_dir) if raw_work_dir.startswith("ssh://")
                         else Path(raw_work_dir).expanduser().resolve() if raw_work_dir else Path.home())
        self.session_id = safe_name(str(conversation_id or "default")) or "default"
        self.data_dir = data_dir
        self.session_root = resolve_session_root(raw_work_dir, self.session_id, data_dir=data_dir)

    def write_original(
        self,
        *,
        kind: str,
        title: str,
        content: Any,
        source: str = "",
        seq_id: int = 0,
        metadata: dict[str, Any] | None = None,
        extension: str | None = None,
        input_payload: Any = None,
        images: list[str] | None = None,
    ) -> ArchivedContentRecord:
        archive_kind = normalize_archive_kind(kind)
        text = stringify_content(content)
        prepared_images = self._prepare_images(images or [])
        digest = self._content_digest(text, prepared_images)
        content_id = self._content_id(source=source, title=title, digest=digest)
        existing = self.read_record(content_id, kind=archive_kind)
        ext = normalize_extension(extension or detect_extension(source, text))
        target_dir = self.kind_root(archive_kind) / content_id
        target = (target_dir / f"original{ext}").resolve()
        if self.session_root not in target.parents:
            raise ValueError("resolved archive path escaped session root")
        target_dir.mkdir(parents=True, exist_ok=True)
        metadata_payload = dict(getattr(existing, "metadata", {}) or {})
        metadata_payload.update(dict(metadata or {}))
        image_metadata = self._write_prepared_images(target_dir, prepared_images)
        if image_metadata:
            metadata_payload["image_attachments"] = image_metadata
            metadata_payload["image_count"] = len(image_metadata)
            metadata_payload["image_bytes"] = sum(int(item.get("size") or 0) for item in image_metadata)
        if input_payload is not None:
            input_path = target_dir / "input.json"
            atomic_write_text(input_path,
                json.dumps(input_payload, ensure_ascii=False, indent=2),
            )
            metadata_payload["input_ref"] = self.relative_to_work_dir(input_path)

        now = timestamp()
        original_ref = self.relative_to_work_dir(target)
        existing_summary = str(getattr(existing, "summary", "") or "")
        record = ArchivedContentRecord(
            id=content_id,
            kind=archive_kind,
            title=str(title or source or archive_kind),
            source=str(source or ""),
            original_ref=original_ref,
            digest=digest,
            size=len(text),
            token_estimate=estimate_tokens(text),
            views=dict(getattr(existing, "views", {}) or {}),
            status=str(getattr(existing, "status", "") or "original_ready") if existing_summary else "original_ready",
            created_seq=int(getattr(existing, "created_seq", 0) or seq_id or 0),
            updated_seq=max(int(getattr(existing, "updated_seq", 0) or 0), int(seq_id or 0)),
            created_at=str(getattr(existing, "created_at", "") or now),
            updated_at=now,
            metadata=metadata_payload,
        )
        atomic_write_text(target_dir / "meta.pending.json", json.dumps(record.to_dict(), ensure_ascii=False))
        atomic_write_text(target, text)
        self.write_record(record)
        return record

    def ensure_image_attachments(
        self,
        record_or_id: ArchivedContentRecord | str,
        images: list[str] | None,
    ) -> ArchivedContentRecord | None:
        record = record_or_id if isinstance(record_or_id, ArchivedContentRecord) else self.read_record(str(record_or_id))
        if record is None or not images:
            return record
        if self.images_are_restorable(record, expected_count=len(images)):
            return record
        prepared = self._prepare_images(images)
        if not prepared:
            return record

        text = self.read_original(record)
        digest = self._content_digest(text, prepared)
        if digest != record.digest:
            metadata = dict(record.metadata or {})
            input_payload = self._read_json_ref(str(metadata.pop("input_ref", "") or ""))
            for key in ("image_attachments", "image_count", "image_bytes"):
                metadata.pop(key, None)
            return self.write_original(
                kind=record.kind,
                title=record.title,
                content=text,
                source=record.source,
                seq_id=int(record.updated_seq or record.created_seq or 0),
                metadata=metadata,
                extension=Path(record.original_ref).suffix or None,
                input_payload=input_payload,
                images=images,
            )

        metadata = self._write_prepared_images(self.record_dir(record), prepared)
        if not metadata:
            return record
        record.metadata = dict(record.metadata or {})
        record.metadata["image_attachments"] = metadata
        record.metadata["image_count"] = len(metadata)
        record.metadata["image_bytes"] = sum(int(item.get("size") or 0) for item in metadata)
        record.updated_at = timestamp()
        self.write_record(record)
        return record

    def _read_json_ref(self, ref: str) -> Any:
        if not ref:
            return None
        try:
            path = self._resolve_session_ref(ref)
            if not path.is_file():
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def read_input(self, record_or_id: ArchivedContentRecord | str) -> Any:
        """Read the archived tool input without exposing storage paths to callers."""
        record = record_or_id if isinstance(record_or_id, ArchivedContentRecord) else self.read_record(str(record_or_id))
        if record is None or not isinstance(record.metadata, dict):
            return None
        return self._read_json_ref(str(record.metadata.get("input_ref") or ""))

    def read_images(self, record_or_id: ArchivedContentRecord | str) -> list[str]:
        record = record_or_id if isinstance(record_or_id, ArchivedContentRecord) else self.read_record(str(record_or_id))
        if record is None:
            return []
        images: list[str] = []
        for item in self._image_metadata(record):
            kind = str(item.get("kind") or "").strip().lower()
            if kind == "url":
                url = str(item.get("url") or "").strip()
                if url:
                    images.append(url)
                continue
            ref = str(item.get("ref") or "").strip()
            mime_type = str(item.get("mime_type") or "image/png").strip() or "image/png"
            if not ref:
                continue
            try:
                path = self._resolve_session_ref(ref)
            except ValueError:
                continue
            if not path.is_file():
                continue
            try:
                encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            except Exception:
                continue
            images.append(f"data:{mime_type};base64,{encoded}")
        return images

    def image_refs(self, record: ArchivedContentRecord) -> list[ContentRef]:
        """Stable references to binary sidecars, owned by this archive record."""
        refs = []
        for index, item in enumerate(self._image_metadata(record), 1):
            if item.get("kind") != "file":
                continue
            identifier = f"{record.id}/images/{index}"
            mime = str(item.get("mime_type") or "image/png")
            refs.append(ContentRef(id=identifier, name=f"image-{index}{mimetypes.guess_extension(mime) or '.png'}",
                mime=mime, size=int(item.get("size") or 0), digest=str(item.get("digest") or ""),
                ref=f"archive:{identifier}", kind="archive", source=record.source,
                workspace=self.workspace, conversation_id=self.session_id, created_at=record.created_at))
        return refs

    def resolve_image(self, record: ArchivedContentRecord, index: int, *, verify_digest: bool = True) -> tuple[Path, ContentRef]:
        identifier = f"{record.id}/images/{index}"
        ref = next((item for item in self.image_refs(record) if item.id == identifier), None)
        if ref is None:
            raise FileNotFoundError(identifier)
        item = self._image_metadata(record)[index - 1]
        path = self._resolve_session_ref(str(item.get("ref") or ""))
        if not path.is_relative_to((self.record_dir(record) / "images").resolve()):
            raise ValueError("archive image escaped its record")
        if not path.is_file():
            raise FileNotFoundError(identifier)
        if verify_digest:
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if path.stat().st_size != ref.size or digest != ref.digest:
                raise ValueError("archive image changed; the pinned version is unavailable")
        return path, ref

    def images_are_restorable(
        self,
        record_or_id: ArchivedContentRecord | str,
        *,
        expected_count: int = 0,
    ) -> bool:
        record = record_or_id if isinstance(record_or_id, ArchivedContentRecord) else self.read_record(str(record_or_id))
        if record is None:
            return False
        metadata = self._image_metadata(record)
        if expected_count <= 0:
            return True
        if len(metadata) < expected_count:
            return False
        for item in metadata[:expected_count]:
            kind = str(item.get("kind") or "").strip().lower()
            if kind == "url":
                if not str(item.get("url") or "").strip():
                    return False
                continue
            ref = str(item.get("ref") or "").strip()
            if not ref:
                return False
            try:
                path = self._resolve_session_ref(ref)
            except ValueError:
                return False
            if not path.is_file():
                return False
        return True

    def write_summary_view(
        self,
        record: ArchivedContentRecord,
        *,
        summary: str,
        source: str = "capability__compress",
        model: str = "",
        confidence: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> ArchivedContentRecord:
        text = str(summary or "").strip()
        record.views = dict(record.views or {})
        if text:
            record.views["summary"] = ArchivedContentView(
                label="summary",
                text=text,
                source=source,
                model=model,
                created_at=timestamp(),
                confidence=float(confidence or 0.0),
                metadata=dict(metadata or {}),
            )
            record.status = "summary_ready"
        else:
            record.status = "summary_failed"
            if metadata:
                record.metadata.update(dict(metadata))
        record.updated_at = timestamp()
        self.write_record(record)
        return record

    def mark_summary_failed(self, record: ArchivedContentRecord, *, error: str = "") -> ArchivedContentRecord:
        record.status = "summary_failed"
        record.updated_at = timestamp()
        record.metadata = dict(record.metadata or {})
        record.metadata["summary_attempted_at"] = record.updated_at
        try:
            record.metadata["summary_failure_count"] = int(record.metadata.get("summary_failure_count") or 0) + 1
        except Exception:
            record.metadata["summary_failure_count"] = 1
        if error:
            record.metadata["summary_error"] = str(error)
        self.write_record(record)
        return record

    def read_original(self, record_or_id: ArchivedContentRecord | str) -> str:
        if isinstance(record_or_id, ArchivedContentRecord):
            record = record_or_id
        else:
            record = self.read_record(str(record_or_id))
        if record is None:
            raise FileNotFoundError(str(record_or_id))
        path = self._resolve_session_ref(record.original_ref)
        if not path.is_file():
            raise FileNotFoundError(str(record_or_id))
        with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
            return fh.read()

    def read_record(self, content_id: str, *, kind: str | None = None) -> ArchivedContentRecord | None:
        content_id = str(content_id or "").strip()
        if not content_id:
            return None
        kinds = [normalize_archive_kind(kind)] if kind else list(ARCHIVE_KINDS)
        for archive_kind in kinds:
            meta = self._record_meta_path(archive_kind, content_id)
            if meta is not None:
                pending = meta.with_name("meta.pending.json")
                if pending.is_file():
                    try:
                        record = ArchivedContentRecord.from_dict(json.loads(pending.read_text(encoding="utf-8")))
                        if record.id == content_id and self.original_matches(record, self.read_original(record)):
                            return record
                    except (OSError, ValueError, TypeError):
                        pass
            if meta is None or not meta.is_file():
                continue
            try:
                return ArchivedContentRecord.from_dict(json.loads(meta.read_text(encoding="utf-8")))
            except Exception:
                continue
        return None

    def list_records(self, *, kind: str | None = None, limit: int | None = None) -> list[ArchivedContentRecord]:
        records: list[ArchivedContentRecord] = []
        kinds = [normalize_archive_kind(kind)] if kind else list(ARCHIVE_KINDS)
        for archive_kind in kinds:
            root = self.kind_root(archive_kind)
            if not root.exists():
                continue
            seen = set()
            for meta in root.glob("*/meta*.json"):
                try:
                    record = self.read_record(meta.parent.name, kind=archive_kind)
                    if record is not None and record.id not in seen:
                        records.append(record)
                        seen.add(record.id)
                except Exception:
                    continue
        records.sort(key=lambda item: (int(item.updated_seq or 0), item.updated_at, item.id), reverse=True)
        if limit is not None:
            return records[: max(0, int(limit or 0))]
        return records

    def write_record(self, record: ArchivedContentRecord) -> None:
        target_dir = self.record_dir(record)
        target_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target_dir / "meta.json", json.dumps(record.to_dict(), ensure_ascii=False, indent=2))
        (target_dir / "meta.pending.json").unlink(missing_ok=True)

    def original_matches(self, record: ArchivedContentRecord, text: str) -> bool:
        """Check original text against its pinned content digest and image manifest."""
        return self._content_digest(text, self._image_metadata(record)) == record.digest

    def record_dir(self, record: ArchivedContentRecord) -> Path:
        return self.kind_root(record.kind) / safe_name(record.id)

    def kind_root(self, kind: str) -> Path:
        return self.session_root / ARCHIVE_KIND_DIRS[normalize_archive_kind(kind)]

    def resolve_ref(self, ref: str) -> Path:
        raw = Path(str(ref or ""))
        if raw.is_absolute():
            return raw
        return (self.work_dir / raw).resolve()

    def _resolve_session_ref(self, ref: str) -> Path:
        path = self.resolve_ref(ref)
        if not _is_within(self.session_root, path):
            raise ValueError("archive reference escaped session root")
        return path

    def _record_meta_path(self, kind: str, content_id: str) -> Path | None:
        value = str(content_id or "").strip()
        if not _is_safe_content_id(value):
            return None
        root = self.kind_root(kind).resolve()
        meta = (root / value / "meta.json").resolve()
        return meta if _is_within(root, meta) else None

    def relative_to_work_dir(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.work_dir).as_posix()
        except Exception:
            return path.as_posix()

    def _content_id(self, *, source: str, title: str, digest: str) -> str:
        prefix = safe_name(source or title or "content").replace("__", "_")[:32] or "content"
        return f"{prefix}-{digest[:12]}"

    @staticmethod
    def _content_digest(text: str, images: list[dict[str, Any]]) -> str:
        digest = hashlib.sha256()
        digest.update(str(text or "").encode("utf-8", errors="replace"))
        for item in images:
            digest.update(b"\0image\0")
            digest.update(str(item.get("digest") or item.get("url") or "").encode("utf-8", errors="replace"))
        return digest.hexdigest()

    @staticmethod
    def _prepare_images(images: list[str]) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        for raw in images or []:
            value = str(raw or "").strip()
            if not value:
                continue
            if value.startswith(("http://", "https://")):
                prepared.append(
                    {
                        "kind": "url",
                        "url": value,
                        "digest": hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest(),
                        "size": 0,
                    }
                )
                continue
            match = re.match(r"^data:([^;,]+);base64,(.*)$", value, flags=re.IGNORECASE | re.DOTALL)
            if not match:
                continue
            mime_type = str(match.group(1) or "image/png").strip().lower() or "image/png"
            if not mime_type.startswith("image/"):
                continue
            try:
                payload = base64.b64decode(match.group(2), validate=False)
            except Exception:
                continue
            if not payload:
                continue
            prepared.append(
                {
                    "kind": "file",
                    "mime_type": mime_type,
                    "data": payload,
                    "digest": hashlib.sha256(payload).hexdigest(),
                    "size": len(payload),
                }
            )
        return prepared

    def _write_prepared_images(self, target_dir: Path, images: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not images:
            return []
        image_dir = (target_dir / "images").resolve()
        if self.session_root not in image_dir.parents:
            raise ValueError("resolved archive image path escaped session root")
        image_dir.mkdir(parents=True, exist_ok=True)
        metadata: list[dict[str, Any]] = []
        for index, item in enumerate(images):
            kind = str(item.get("kind") or "").strip().lower()
            if kind == "url":
                metadata.append(
                    {
                        "kind": "url",
                        "url": str(item.get("url") or ""),
                        "digest": str(item.get("digest") or ""),
                        "size": 0,
                    }
                )
                continue
            payload = item.get("data")
            if not isinstance(payload, (bytes, bytearray)):
                continue
            mime_type = str(item.get("mime_type") or "image/png").strip() or "image/png"
            extension = mimetypes.guess_extension(mime_type) or ".bin"
            digest = str(item.get("digest") or hashlib.sha256(payload).hexdigest())
            path = (image_dir / f"{index:03d}-{digest[:12]}{extension}").resolve()
            if self.session_root not in path.parents:
                raise ValueError("resolved archive image path escaped session root")
            atomic_write_bytes(path, bytes(payload))
            metadata.append(
                {
                    "kind": "file",
                    "ref": self.relative_to_work_dir(path),
                    "mime_type": mime_type,
                    "digest": digest,
                    "size": len(payload),
                }
            )
        return metadata

    @staticmethod
    def _image_metadata(record: ArchivedContentRecord) -> list[dict[str, Any]]:
        raw = (record.metadata or {}).get("image_attachments") if isinstance(record.metadata, dict) else None
        return [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def safe_name(value: object) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value or "")).strip("._-")[:100]


def _is_safe_content_id(value: str) -> bool:
    """Accept only one generated archive directory name."""

    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", str(value or "")))


def _is_within(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_extension(extension: str) -> str:
    raw = str(extension or ".txt").strip()
    if not raw:
        return ".txt"
    if not raw.startswith("."):
        raw = "." + raw
    return raw[:20]


def detect_extension(source: str, text: str) -> str:
    raw = str(text or "").lstrip()
    name = str(source or "")
    if raw[:1] in {"{", "["}:
        return ".json"
    if "<html" in raw[:2000].lower() or "<!doctype html" in raw[:2000].lower():
        return ".html"
    guessed, _ = mimetypes.guess_type(name)
    if guessed == "text/markdown":
        return ".md"
    return ".txt"


def stringify_content(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (dict, list)):
        try:
            return json.dumps(raw, ensure_ascii=False, indent=2)
        except Exception:
            return str(raw)
    return str(raw or "")
