from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


ARCHIVE_KINDS: tuple[str, ...] = ("tool_call", "history")
FILE_CHANGE_ACTIONS: tuple[str, ...] = ("write", "edit", "patch", "delete")
FILE_CHANGE_STATUSES: tuple[str, ...] = ("completed", "failed", "cancelled")


@dataclass(frozen=True)
class ContentRef:
    """Lightweight navigation reference to content owned by another service."""

    id: str
    name: str
    mime: str
    size: int
    digest: str
    ref: str
    kind: str = "input"
    source: str = "user"
    status: str = "ready"
    message_id: str = ""
    created_at: str = ""
    workspace: str = ""
    conversation_id: str = ""
    locator: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "mime": self.mime,
            "size": int(self.size or 0),
            "digest": self.digest,
            "ref": self.ref,
            "source": self.source,
            "status": self.status,
            "message_id": self.message_id,
            "created_at": self.created_at,
            "workspace": self.workspace,
            "conversation_id": self.conversation_id,
            "locator": self.locator,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ContentRef":
        payload = data if isinstance(data, dict) else {}
        return cls(
            id=str(payload.get("id") or ""),
            kind=str(payload.get("kind") or "input"),
            name=str(payload.get("name") or "attachment"),
            mime=str(payload.get("mime") or "application/octet-stream"),
            size=int(payload.get("size", 0) or 0),
            digest=str(payload.get("digest") or ""),
            ref=str(payload.get("ref") or (f"{payload['kind']}:{payload['id']}" if payload.get("kind") and payload.get("id") else "")),
            source=str(payload.get("source") or "user"),
            status=str(payload.get("status") or "ready"),
            message_id=str(payload.get("message_id") or ""),
            created_at=str(payload.get("created_at") or ""),
            workspace=str(payload.get("workspace") or ""),
            conversation_id=str(payload.get("conversation_id") or ""),
            locator=str(payload.get("locator") or ""),
        )


@dataclass(frozen=True)
class MaterialPage:
    """A bounded read projection; content remains with its domain owner."""

    items: list[dict[str, Any]]
    total: int
    offset: int
    limit: int

    @property
    def has_next(self) -> bool:
        return self.offset + len(self.items) < self.total


@dataclass(frozen=True)
class FileChange:
    """A completed workspace mutation emitted by a file tool.

    The contract is intentionally small.  It records the mutation fact used by
    Chat and Inspector; it does not own the file, watch the workspace, or
    duplicate file contents.
    """

    change_id: str = ""
    path: str = ""
    action: str = "edit"
    before_digest: str = ""
    after_digest: str = ""
    run_id: str = ""
    tool_call_id: str = ""
    created_at: str = ""
    status: str = "completed"
    summary: str = ""

    def __post_init__(self) -> None:
        action = str(self.action or "edit").strip().lower()
        status = str(self.status or "completed").strip().lower()
        if action not in FILE_CHANGE_ACTIONS:
            raise ValueError(f"unsupported file change action: {self.action!r}")
        if status not in FILE_CHANGE_STATUSES:
            raise ValueError(f"unsupported file change status: {self.status!r}")
        object.__setattr__(self, "change_id", str(self.change_id or "").strip())
        object.__setattr__(self, "path", str(self.path or "").strip())
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "before_digest", str(self.before_digest or "").strip())
        object.__setattr__(self, "after_digest", str(self.after_digest or "").strip())
        object.__setattr__(self, "run_id", str(self.run_id or "").strip())
        object.__setattr__(self, "tool_call_id", str(self.tool_call_id or "").strip())
        object.__setattr__(self, "created_at", str(self.created_at or "").strip())
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "summary", str(self.summary or "").strip())

    @property
    def is_successful(self) -> bool:
        return self.status == "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_id": self.change_id,
            "path": self.path,
            "action": self.action,
            "before_digest": self.before_digest,
            "after_digest": self.after_digest,
            "run_id": self.run_id,
            "tool_call_id": self.tool_call_id,
            "created_at": self.created_at,
            "status": self.status,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "FileChange":
        payload = data if isinstance(data, dict) else {}
        return cls(
            change_id=str(payload.get("change_id") or payload.get("id") or ""),
            path=str(payload.get("path") or payload.get("relative_path") or ""),
            action=str(payload.get("action") or "edit"),
            before_digest=str(payload.get("before_digest") or ""),
            after_digest=str(payload.get("after_digest") or ""),
            run_id=str(payload.get("run_id") or ""),
            tool_call_id=str(payload.get("tool_call_id") or ""),
            created_at=str(payload.get("created_at") or ""),
            status=str(payload.get("status") or "completed"),
            summary=str(payload.get("summary") or ""),
        )


@dataclass(frozen=True)
class InputPreparationFailure:
    source: str
    error: str


@dataclass(frozen=True)
class InputPreparationResult:
    refs: list[ContentRef] = field(default_factory=list)
    failures: list[InputPreparationFailure] = field(default_factory=list)
    created_refs: list[ContentRef] = field(default_factory=list)


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
    status: str = "original_ready"
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
            status=str(payload.get("status") or "original_ready"),
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

    @property
    def summary_status(self) -> str:
        if self.summary:
            return "complete"
        if self.status in {"summary_failed", "failed"}:
            return "failed"
        if self.status in {"summary_pending", "pending"}:
            return "pending"
        return "none"

    @property
    def references(self) -> list[str]:
        meta = self.metadata if isinstance(self.metadata, dict) else {}
        return [str(item) for item in (meta.get("references") or []) if str(item).strip()]

    @property
    def sections(self) -> list[dict[str, Any]]:
        meta = self.metadata if isinstance(self.metadata, dict) else {}
        raw = meta.get("sections")
        return [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def to_index_dict(self) -> dict[str, Any]:
        """Serialize the lightweight state/prompt index view."""
        summary = trim_index_summary(str(self.summary or ""))
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "source": self.source,
            "original_ref": self.original_ref,
            "digest": self.digest,
            "size": int(self.size or 0),
            "token_estimate": int(self.token_estimate or 0),
            "status": self.status,
            "summary_status": self.summary_status,
            "summary_excerpt": summary,
            "created_seq": int(self.created_seq or 0),
            "updated_seq": int(self.updated_seq or 0),
            "updated_at": self.updated_at,
            "references": list(self.references or [])[:8],
        }

    def to_index_record(self) -> "ArchivedContentRecord":
        """Return an ArchivedContentRecord carrying only index-safe fields."""
        summary = trim_index_summary(str(self.summary or ""))
        views: dict[str, ArchivedContentView] = {}
        if summary:
            views["summary"] = ArchivedContentView(label="summary", text=summary)
        return ArchivedContentRecord(
            id=str(self.id or ""),
            kind=normalize_archive_kind(self.kind or "tool_call"),
            title=str(self.title or "content"),
            source=str(self.source or ""),
            original_ref=str(self.original_ref or ""),
            digest=str(self.digest or ""),
            size=int(self.size or 0),
            token_estimate=int(self.token_estimate or 0),
            views=views,
            status=str(self.status or "original_ready"),
            created_seq=int(self.created_seq or 0),
            updated_seq=int(self.updated_seq or 0),
            created_at=str(self.created_at or ""),
            updated_at=str(self.updated_at or ""),
            metadata={"references": list(self.references or [])[:8]},
        )

    @classmethod
    def from_index_dict(cls, data: dict[str, Any] | None) -> "ArchivedContentRecord":
        payload = data if isinstance(data, dict) else {}
        if "views" in payload or "metadata" in payload:
            return cls.from_dict(payload).to_index_record()
        summary = str(payload.get("summary_excerpt") or payload.get("summary") or "")[:1200]
        views: dict[str, ArchivedContentView] = {}
        if summary:
            views["summary"] = ArchivedContentView(label="summary", text=summary)
        return cls(
            id=str(payload.get("id") or payload.get("content_id") or ""),
            kind=normalize_archive_kind(payload.get("kind") or "tool_call"),
            title=str(payload.get("title") or "content"),
            source=str(payload.get("source") or ""),
            original_ref=str(payload.get("original_ref") or ""),
            digest=str(payload.get("digest") or ""),
            size=int(payload.get("size", 0) or 0),
            token_estimate=int(payload.get("token_estimate", 0) or 0),
            views=views,
            status=str(payload.get("status") or "original_ready"),
            created_seq=int(payload.get("created_seq", 0) or 0),
            updated_seq=int(payload.get("updated_seq", 0) or 0),
            updated_at=str(payload.get("updated_at") or ""),
            metadata={"references": [str(item) for item in (payload.get("references") or []) if str(item).strip()][:8]},
        )


def normalize_archive_kind(kind: object) -> str:
    raw = str(kind or "tool_call").strip().lower().replace("-", "_")
    if raw in {"tool", "tool_result", "tool_call"}:
        return "tool_call"
    if raw in {"history", "conversation"}:
        return "history"
    if raw not in ARCHIVE_KINDS:
        raise ValueError(f"unsupported archive kind: {kind}")
    return raw


def trim_index_summary(text: str, limit: int = 1200) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 3)].rstrip() + "..."
