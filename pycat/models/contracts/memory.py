"""Durable memory entry metadata, independent of storage and UI."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MemoryEntry:
    id: str
    text: str
    sources: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    updated_at: str = ""
    origin: str = "direct"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "sources": [dict(ref) for ref in self.sources],
                "updated_at": self.updated_at, "origin": self.origin}
