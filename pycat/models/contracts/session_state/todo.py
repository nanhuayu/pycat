from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable

RECENT_COMPLETED_TODO_LIMIT = 5


class TodoStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


def _status(value: Any, default: TodoStatus = TodoStatus.PENDING) -> TodoStatus:
    raw = str(getattr(value, "value", value) or default.value).strip().lower()
    return TodoStatus(raw) if raw in {item.value for item in TodoStatus} else default


def _legacy_note(data: Dict[str, Any]) -> str:
    direct = str(data.get("note") or "").strip()
    if direct:
        return direct
    parts: list[str] = []
    description = str(data.get("description") or "").strip()
    acceptance = str(data.get("acceptance") or "").strip()
    blocked = str(data.get("blocked_reason") or "").strip()
    if description:
        parts.append(description)
    if acceptance:
        parts.append(f"Acceptance: {acceptance}")
    if blocked:
        parts.append(f"Blocked: {blocked}")
    return "; ".join(parts)


def _refs(values: Iterable[Any] | None) -> list[str]:
    result: list[str] = []
    for item in values or ():
        value = str(item or "").strip()
        if value and value not in result:
            result.append(value)
        if len(result) >= 12:
            break
    return result


@dataclass
class TodoItem:
    title: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    status: TodoStatus = TodoStatus.PENDING
    note: str = ""
    refs: list[str] = field(default_factory=list)
    created_seq: int = 0
    updated_seq: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status.value,
            "note": self.note,
            "refs": list(self.refs),
            "created_seq": int(self.created_seq or 0),
            "updated_seq": int(self.updated_seq or 0),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TodoItem":
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:8]).strip(),
            title=str(data.get("title") or "").strip(),
            status=_status(data.get("status")),
            note=_legacy_note(data),
            refs=_refs(data.get("refs") or data.get("evidence_refs")),
            created_seq=int(data.get("created_seq", 0) or 0),
            updated_seq=int(data.get("updated_seq", 0) or 0),
        )

    def update(self, current_seq: int, **values: Any) -> None:
        if values.get("title") is not None:
            self.title = str(values["title"]).strip()
        if values.get("status") is not None:
            self.status = _status(values["status"], self.status)
        if values.get("note") is not None:
            self.note = str(values["note"]).strip()
        if values.get("refs") is not None:
            self.refs = _refs(values["refs"])
        self.updated_seq = int(current_seq or 0)


@dataclass
class TodoDigest:
    title: str
    id: str = ""
    status: TodoStatus = TodoStatus.COMPLETED
    note: str = ""
    refs: list[str] = field(default_factory=list)
    completed_seq: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status.value,
            "note": self.note,
            "refs": list(self.refs),
            "completed_seq": int(self.completed_seq or 0),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TodoDigest":
        return cls(
            id=str(data.get("id") or "").strip(),
            title=str(data.get("title") or "").strip(),
            status=_status(data.get("status"), TodoStatus.COMPLETED),
            note=_legacy_note(data),
            refs=_refs(data.get("refs") or data.get("evidence_refs")),
            completed_seq=int(data.get("completed_seq", 0) or 0),
        )
