from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List
import uuid

WORK_TRACE_STEP_LIMIT = 32


@dataclass
class WorkTraceStep:
    """Runtime-maintained summary of one meaningful execution step.

    Work trace is not memory and is not written by the model. It gives the
    next prompt a compact route through recent tool work without replaying raw
    tool results.
    """

    seq: int = 0
    turn: int = 0
    kind: str = "tool"
    label: str = ""
    tool_name: str = ""
    target: str = ""
    status: str = "completed"
    summary: str = ""
    refs: List[str] = field(default_factory=list)
    content_id: str = ""
    chars: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'seq': int(self.seq or 0),
            'turn': int(self.turn or 0),
            'kind': self.kind,
            'label': self.label,
            'tool_name': self.tool_name,
            'target': self.target,
            'status': self.status,
            'summary': self.summary,
            'refs': list(self.refs),
            'content_id': self.content_id,
            'chars': int(self.chars or 0),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'WorkTraceStep':
        payload = data if isinstance(data, dict) else {}
        return cls(
            seq=int(payload.get('seq', 0) or 0),
            turn=int(payload.get('turn', 0) or 0),
            kind=str(payload.get('kind') or 'tool'),
            label=str(payload.get('label') or ''),
            tool_name=str(payload.get('tool_name') or ''),
            target=str(payload.get('target') or ''),
            status=str(payload.get('status') or 'completed'),
            summary=str(payload.get('summary') or ''),
            refs=[str(item) for item in (payload.get('refs') or []) if str(item).strip()],
            content_id=str(payload.get('content_id') or ''),
            chars=int(payload.get('chars', 0) or 0),
        )


@dataclass
class WorkTrace:
    """Short-lived runtime route for the current work.

    Summary is historical compression; memory is durable facts. WorkTrace is
    the live "how we got here" route assembled from tool events.
    """

    goal: str = ""
    phase: str = ""
    steps: List[WorkTraceStep] = field(default_factory=list)
    updated_seq: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'goal': self.goal,
            'phase': self.phase,
            'steps': [step.to_dict() for step in self.steps[-WORK_TRACE_STEP_LIMIT:]],
            'updated_seq': int(self.updated_seq or 0),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'WorkTrace':
        payload = data if isinstance(data, dict) else {}
        return cls(
            goal=str(payload.get('goal') or ''),
            phase=str(payload.get('phase') or ''),
            steps=[
                WorkTraceStep.from_dict(item)
                for item in (payload.get('steps') or [])
                if isinstance(item, dict)
            ][-WORK_TRACE_STEP_LIMIT:],
            updated_seq=int(payload.get('updated_seq', 0) or 0),
        )



