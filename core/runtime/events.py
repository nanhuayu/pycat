from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from core.task.types import TaskEvent, TaskEventKind


class TurnEventKind(str, Enum):
    TURN_START = "turn_start"
    TOKEN = "token"
    THINKING = "thinking"
    STEP = "step"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    RETRY = "retry"
    CONDENSE = "condense"
    COMPLETE = "complete"
    ERROR = "error"


@dataclass(frozen=True)
class TurnEvent:
    kind: TurnEventKind
    data: Any = None
    turn: int = 0
    detail: str = ""
    source: str = "task"

    @property
    def tool_name(self) -> str:
        if isinstance(self.data, dict):
            return str(self.data.get("tool_name") or self.data.get("name") or "").strip()
        return ""

    @property
    def phase(self) -> str:
        if isinstance(self.data, dict):
            return str(self.data.get("phase") or "").strip()
        return ""

    @classmethod
    def from_task_event(cls, event: TaskEvent) -> "TurnEvent":
        return cls(
            kind=TurnEventKind(getattr(event.kind, "value", str(event.kind))),
            data=event.data,
            turn=int(getattr(event, "turn", 0) or 0),
            detail=str(getattr(event, "detail", "") or ""),
            source="task",
        )

    def to_task_event(self) -> TaskEvent:
        return TaskEvent(
            kind=TaskEventKind(getattr(self.kind, "value", str(self.kind))),
            data=self.data,
            turn=int(self.turn or 0),
            detail=str(self.detail or ""),
        )
