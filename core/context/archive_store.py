"""Session archive storage for recoverable context content.

Archived content keeps the original bytes/text as the source of truth. Summary
and other compressed views are derived data written beside the original and can
always be regenerated.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


ARCHIVE_KINDS: tuple[str, ...] = ("tool_call", "history", "artifact")
ARCHIVE_KIND_DIRS: dict[str, str] = {
    "tool_call": "tool-call",
    "history": "history",
    "artifact": "artifact",
}


def estimate_tokens(text: str) -> int:
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", str(text or "")))
    other_chars = len(str(text or "")) - chinese_chars
    return int(chinese_chars + other_chars * 0.25)


@dataclass
class ArchivedContentView:
    label: str
    text: str = ""
    source: str = ""
    model: str = ""
    created_at: str = ""
    confidence: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "text": self.text,
            "source": self.source,
            "model": self.model,
            "created_at": self.created_at,
            "confidence": float(self.confidence or 0.0),
            "metadata": dict(self.metadata or {}),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ArchivedContentView":
        payload = data if isinstance(data, dict) else {}
        return cls(
            label=str(payload.get("label") or ""),
            text=str(payload.get("text") or ""),
            source=str(payload.get("source") or ""),
            model=str(payload.get("model") or ""),
            created_at=str(payload.get("created_at") or ""),
            confidence=float(payload.get("confidence") or 0.0),
            metadata=dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else {},
        )


@dataclass
class ArchivedContentRecord:
    id: str
    kind: str
    title: str
    source: str
    original_ref: str
    digest: str
    size: int = 0
    token_estimate: int = 0
    views: dict[str, ArchivedContentView] = field(default_factory=dict)
    status: str = "summary_pending"
    created_seq: int = 0
    updated_seq: int = 0
    created_at: str = ""
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "source": self.source,
            "original_ref": self.original_ref,
            "digest": self.digest,
            "size": int(self.size or 0),
            "token_estimate": int(self.token_estimate or 0),
            "views": {k: v.to_dict() for k, v in (self.views or {}).items()},
            "status": self.status,
            "created_seq": int(self.created_seq or 0),
            "updated_seq": int(self.updated_seq or 0),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata or {}),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ArchivedContentRecord":
        payload = data if isinstance(data, dict) else {}
        views_raw = payload.get("views") if isinstance(payload.get("views"), dict) else {}
        return cls(
            id=str(payload.get("id") or payload.get("content_id") or ""),
            kind=normalize_archive_kind(payload.get("kind") or "tool_call"),
            title=str(payload.get("title") or "content"),
            source=str(payload.get("source") or ""),
            original_ref=str(payload.get("original_ref") or ""),
            digest=str(payload.get("digest") or ""),
            size=int(payload.get("size", 0) or 0),
            token_estimate=int(payload.get("token_estimate", 0) or 0),
            views={
                str(key): ArchivedContentView.from_dict(value)
                for key, value in views_raw.items()
                if isinstance(value, dict)
            },
            status=str(payload.get("status") or "summary_pending"),
            created_seq=int(payload.get("created_seq", 0) or 0),
            updated_seq=int(payload.get("updated_seq", 0) or 0),
            created_at=str(payload.get("created_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
            metadata=dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else {},
        )

    @property
    def summary(self) -> str:
        view = (self.views or {}).get("summary")
        return str(view.text or "") if view else ""

    def view_text(self, key: str) -> str:
        view = (self.views or {}).get(str(key or "").strip())
        return str(view.text or "") if view else ""

    @property
    def summary_status(self) -> str:
        if self.summary:
            return "complete"
        if self.status in {"summary_failed", "failed"}:
            return "failed"
        return "pending"

    @property
    def references(self) -> list[str]:
        meta = self.metadata if isinstance(self.metadata, dict) else {}
        return [str(item) for item in (meta.get("references") or []) if str(item).strip()]

    @property
    def sections(self) -> list[dict[str, Any]]:
        meta = self.metadata if isinstance(self.metadata, dict) else {}
        raw = meta.get("sections")
        return [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


class SessionArchiveStore:
    """Read/write recoverable content under one conversation session."""

    INDEX_NAME = "index.json"

    def __init__(self, work_dir: str, conversation_id: object = None):
        self.work_dir = Path(work_dir or ".").expanduser().resolve()
        self.session_id = safe_name(str(conversation_id or "default")) or "default"
        self.session_root = self.work_dir / ".pycat" / "sessions" / self.session_id

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
    ) -> ArchivedContentRecord:
        archive_kind = normalize_archive_kind(kind)
        text = stringify_content(content)
        digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
        content_id = self._content_id(source=source, title=title, digest=digest)
        ext = normalize_extension(extension or detect_extension(source, text))
        target_dir = self.kind_root(archive_kind) / content_id
        target = (target_dir / f"original{ext}").resolve()
        if self.session_root not in target.parents:
            raise ValueError("resolved archive path escaped session root")
        target_dir.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", errors="replace", newline="") as fh:
            fh.write(text)
        metadata_payload = dict(metadata or {})
        if input_payload is not None:
            input_path = target_dir / "input.json"
            input_path.write_text(
                json.dumps(input_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            metadata_payload["input_ref"] = self.relative_to_work_dir(input_path)

        now = timestamp()
        original_ref = self.relative_to_work_dir(target)
        record = ArchivedContentRecord(
            id=content_id,
            kind=archive_kind,
            title=str(title or source or archive_kind),
            source=str(source or ""),
            original_ref=original_ref,
            digest=digest,
            size=len(text),
            token_estimate=estimate_tokens(text),
            views={},
            status="summary_pending",
            created_seq=int(seq_id or 0),
            updated_seq=int(seq_id or 0),
            created_at=now,
            updated_at=now,
            metadata=metadata_payload,
        )
        self.write_record(record)
        return record

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

    def write_view(
        self,
        record: ArchivedContentRecord,
        *,
        key: str,
        label: str,
        text: str,
        source: str = "capability__compress",
        model: str = "",
        confidence: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> ArchivedContentRecord:
        clean_key = str(key or "").strip()
        clean_text = str(text or "").strip()
        if not clean_key:
            raise ValueError("archive view key is required")
        record.views = dict(record.views or {})
        if clean_text:
            record.views[clean_key] = ArchivedContentView(
                label=str(label or clean_key),
                text=clean_text,
                source=source,
                model=model,
                created_at=timestamp(),
                confidence=float(confidence or 0.0),
                metadata=dict(metadata or {}),
            )
            if clean_key == "summary":
                record.status = "summary_ready"
        elif clean_key == "summary":
            record.status = "summary_failed"
            if metadata:
                record.metadata.update(dict(metadata))
        record.updated_at = timestamp()
        self.write_record(record)
        return record

    def mark_summary_failed(self, record: ArchivedContentRecord, *, error: str = "") -> ArchivedContentRecord:
        record.status = "summary_failed"
        record.updated_at = timestamp()
        if error:
            record.metadata = dict(record.metadata or {})
            record.metadata["summary_error"] = str(error)
        self.write_record(record)
        return record

    def read_original(self, record_or_id: ArchivedContentRecord | str) -> str:
        record = record_or_id if isinstance(record_or_id, ArchivedContentRecord) else self.read_record(str(record_or_id))
        if record is None:
            raise FileNotFoundError(str(record_or_id))
        path = self.resolve_ref(record.original_ref)
        with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
            return fh.read()

    def read_record(self, content_id: str, *, kind: str | None = None) -> ArchivedContentRecord | None:
        content_id = str(content_id or "").strip()
        if not content_id:
            return None
        kinds = [normalize_archive_kind(kind)] if kind else list(ARCHIVE_KINDS)
        for archive_kind in kinds:
            meta = self.kind_root(archive_kind) / content_id / "meta.json"
            if not meta.exists():
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
            for meta in root.glob("*/meta.json"):
                try:
                    records.append(ArchivedContentRecord.from_dict(json.loads(meta.read_text(encoding="utf-8"))))
                except Exception:
                    continue
        records.sort(key=lambda item: (int(item.updated_seq or 0), item.updated_at, item.id), reverse=True)
        if limit is not None:
            return records[: max(0, int(limit or 0))]
        return records

    def write_record(self, record: ArchivedContentRecord) -> None:
        target_dir = self.record_dir(record)
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "meta.json").write_text(json.dumps(record.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        views = {key: view.to_dict() for key, view in (record.views or {}).items()}
        (target_dir / "views.json").write_text(json.dumps(views, ensure_ascii=False, indent=2), encoding="utf-8")
        self.write_index()

    def write_index(self) -> None:
        records = [record.to_dict() for record in self.list_records()]
        self.session_root.mkdir(parents=True, exist_ok=True)
        (self.session_root / self.INDEX_NAME).write_text(json.dumps({"archive": records}, ensure_ascii=False, indent=2), encoding="utf-8")

    def record_dir(self, record: ArchivedContentRecord) -> Path:
        return self.kind_root(record.kind) / safe_name(record.id)

    def kind_root(self, kind: str) -> Path:
        return self.session_root / ARCHIVE_KIND_DIRS[normalize_archive_kind(kind)]

    def resolve_ref(self, ref: str) -> Path:
        raw = Path(str(ref or ""))
        if raw.is_absolute():
            return raw
        return (self.work_dir / raw).resolve()

    def relative_to_work_dir(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.work_dir).as_posix()
        except Exception:
            return path.as_posix()

    def _content_id(self, *, source: str, title: str, digest: str) -> str:
        prefix = safe_name(source or title or "content").replace("__", "_")[:32] or "content"
        return f"{prefix}-{digest[:12]}"


def normalize_archive_kind(kind: object) -> str:
    raw = str(kind or "tool_call").strip().lower().replace("-", "_")
    if raw in {"tool", "tool_result", "tool_call"}:
        return "tool_call"
    if raw in {"history", "conversation"}:
        return "history"
    if raw in {"artifact", "artifacts"}:
        return "artifact"
    return raw if raw in ARCHIVE_KINDS else "tool_call"


def safe_name(value: object) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value or "")).strip("._-")[:100]


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
