from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable


MEMORY_CONTENT_LIMIT = 600
MEMORY_TOPIC_CONTENT_LIMIT = 12_000
MEMORY_CATEGORIES = {"preference", "fact", "decision", "convention", "command", "gotcha"}
MEMORY_SCOPES = {"session", "workspace", "global"}
MEMORY_CANDIDATE_STATUSES = {"pending", "promoted", "rejected"}


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
class MemoryRecord:
    key: str
    content: str
    scope: str = "session"
    category: str = "fact"
    refs: list[str] = field(default_factory=list)
    updated_seq: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "content": self.content,
            "scope": self.scope,
            "category": self.category,
            "refs": list(self.refs),
            "updated_seq": int(self.updated_seq or 0),
        }

    @classmethod
    def from_dict(cls, key: str, data: Any) -> "MemoryRecord":
        payload = data if isinstance(data, dict) else {}
        category = str(payload.get("category") or "fact").strip().lower()
        scope = str(payload.get("scope") or "session").strip().lower()
        return cls(
            key=str(payload.get("key") or key or "").strip(),
            content=str(payload.get("content") or payload.get("value") or str(data or "")).strip()[:MEMORY_CONTENT_LIMIT],
            scope=scope if scope in MEMORY_SCOPES else "session",
            category=category if category in MEMORY_CATEGORIES else "fact",
            refs=_refs(payload.get("refs") or payload.get("evidence_refs")),
            updated_seq=int(payload.get("updated_seq", 0) or 0),
        )


@dataclass
class MemoryCandidate:
    id: str
    content: str
    scope: str = "session"
    category: str = "fact"
    reason: str = ""
    refs: list[str] = field(default_factory=list)
    status: str = "pending"
    created_seq: int = 0
    updated_seq: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "scope": self.scope,
            "category": self.category,
            "reason": self.reason,
            "refs": list(self.refs),
            "status": self.status,
            "created_seq": int(self.created_seq or 0),
            "updated_seq": int(self.updated_seq or 0),
        }

    @classmethod
    def from_dict(cls, candidate_id: str, data: Any) -> "MemoryCandidate":
        payload = data if isinstance(data, dict) else {}
        scope = str(payload.get("scope") or "session").strip().lower()
        category = str(payload.get("category") or "fact").strip().lower()
        status = str(payload.get("status") or "pending").strip().lower()
        return cls(
            id=str(payload.get("id") or candidate_id or "").strip(),
            content=str(payload.get("content") or "").strip()[:MEMORY_CONTENT_LIMIT],
            scope=scope if scope in MEMORY_SCOPES else "session",
            category=category if category in MEMORY_CATEGORIES else "fact",
            reason=str(payload.get("reason") or "").strip()[:MEMORY_CONTENT_LIMIT],
            refs=_refs(payload.get("refs") or payload.get("evidence_refs")),
            status=status if status in MEMORY_CANDIDATE_STATUSES else "pending",
            created_seq=int(payload.get("created_seq", 0) or 0),
            updated_seq=int(payload.get("updated_seq", 0) or 0),
        )
