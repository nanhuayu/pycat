from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List

from pycat.models.contracts.content import ArchivedContentRecord
from pycat.models.contracts.session_state.artifact import SessionArtifact
from pycat.models.contracts.session_state.todo import RECENT_COMPLETED_TODO_LIMIT, TodoDigest, TodoItem
from pycat.models.contracts.session_state.work_trace import WorkTrace


def _short_sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:16]

@dataclass
class SessionState:
    """
    The "brain" of a conversation - holds cognitive state separate from message history.

    This replaces the scattered archived_content_id/summary fields with a unified state object.
    Key design decisions:
    - summary: Global rolling summary (replaces is_summary messages)
    - todos: Active model-managed todo list for current progress
    - recent_completed_todos: Short completion trace to prevent todo amnesia
    - artifacts: Explicit model-managed session outputs and working notes
    - work_trace: Runtime-maintained route through current tool work
    - last_updated_seq: Tracks when state was last modified for rollback

    The state is:
    1. Persisted as part of Conversation JSON
    2. Selected by context providers for each LLM request
    3. Updated through the focused state services and tools
    """
    summary: str = ""
    todos: List[TodoItem] = field(default_factory=list)
    recent_completed_todos: List[TodoDigest] = field(default_factory=list)
    artifacts: Dict[str, SessionArtifact] = field(default_factory=dict)
    archive_index: Dict[str, ArchivedContentRecord] = field(default_factory=dict)
    work_trace: WorkTrace = field(default_factory=WorkTrace)
    last_updated_seq: int = 0
    state_version: int = 0
    last_maintenance_seq: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'summary': self.summary,
            'todos': [t.to_dict() for t in self.todos],
            'recent_completed_todos': [t.to_dict() for t in self.recent_completed_todos[-RECENT_COMPLETED_TODO_LIMIT:]],
            'artifacts': {k: v.to_dict() for k, v in self.artifacts.items()},
            'archive_index': {k: self._archive_entry_to_dict(v) for k, v in self.archive_index.items()},
            'work_trace': self.work_trace.to_dict(),
            'last_updated_seq': self.last_updated_seq,
            'state_version': self.state_version,
            'last_maintenance_seq': self.last_maintenance_seq,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SessionState':
        if not data:
            return cls()
        todos = [
            TodoItem.from_dict(t)
            for t in (data.get('todos') or [])
            if isinstance(t, dict)
        ]
        recent_completed_todos = [
            TodoDigest.from_dict(t)
            for t in data.get('recent_completed_todos', [])
            if isinstance(t, dict)
        ][-RECENT_COMPLETED_TODO_LIMIT:]
        artifacts_raw = data.get('artifacts', {})
        artifacts = {
            k: SessionArtifact.from_dict(v)
            for k, v in artifacts_raw.items()
        } if isinstance(artifacts_raw, dict) else {}
        archive_raw = data.get('archive_index', {})
        archive_index = {
            k: ArchivedContentRecord.from_index_dict(v)
            for k, v in archive_raw.items()
            if isinstance(v, dict)
        } if isinstance(archive_raw, dict) else {}
        work_trace = WorkTrace.from_dict(data.get('work_trace', {}))
        return cls(
            summary=data.get('summary', ''),
            todos=todos,
            recent_completed_todos=recent_completed_todos,
            artifacts=artifacts,
            archive_index=archive_index,
            work_trace=work_trace,
            last_updated_seq=data.get('last_updated_seq', 0),
            state_version=int(data.get('state_version', 0) or 0),
            last_maintenance_seq=int(data.get('last_maintenance_seq', 0) or 0),
        )

    def checkpoint(self) -> Dict[str, Any]:
        """Bounded fingerprint of this state for message diagnostics."""
        archive_digest = _short_sha1(json.dumps(
            [
                {
                    "id": getattr(record, "id", ""),
                    "digest": getattr(record, "digest", ""),
                    "updated_seq": getattr(record, "updated_seq", 0),
                }
                for record in (self.archive_index or {}).values()
            ],
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ))
        return {
            "_snapshot_kind": "checkpoint",
            "state_version": int(self.state_version or 0),
            "last_updated_seq": int(self.last_updated_seq or 0),
            "last_maintenance_seq": int(self.last_maintenance_seq or 0),
            "summary_digest": _short_sha1(str(self.summary or "")),
            "archive_count": len(self.archive_index or {}),
            "archive_digest": archive_digest,
            "work_trace_updated_seq": int(getattr(self.work_trace, "updated_seq", 0) or 0),
        }

    @staticmethod
    def _archive_entry_to_dict(record: object) -> Dict[str, Any]:
        if isinstance(record, ArchivedContentRecord):
            return record.to_index_dict()
        if isinstance(record, dict):
            return ArchivedContentRecord.from_index_dict(record).to_index_dict()
        return ArchivedContentRecord.from_index_dict({}).to_index_dict()
