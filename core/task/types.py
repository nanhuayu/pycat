"""Data types for the task execution engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional, Set

from core.config.schema import ToolPermissionConfig
from core.tools.catalog import ToolSelectionPolicy
from models.conversation import Message


# ---------------------------------------------------------------------------
# TaskStatus
# ---------------------------------------------------------------------------
class TaskStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# TaskResult
# ---------------------------------------------------------------------------
@dataclass
class TaskResult:
    status: TaskStatus = TaskStatus.COMPLETED
    final_message: Optional[Message] = None
    error: Optional[str] = None


class SubtaskTraceStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class SubtaskTrace:
    """Persistent, renderable transcript for one delegated child run.

    The child conversation never becomes top-level parent conversation history.
    Instead, its committed messages are serialized here and rendered by the
    same MessageWidget tree inside a collapsible subtask panel.
    """

    id: str
    kind: str = "subagent"
    name: str = "subtask"
    title: str = "Subtask"
    goal: str = ""
    status: SubtaskTraceStatus = SubtaskTraceStatus.RUNNING
    mode: str = "agent"
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    final_message: str = ""
    error: str = ""
    tool_count: int = 0
    token_count: int = 0
    depth: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.started_at:
            self.started_at = datetime.now().isoformat(timespec="seconds")
        if not isinstance(self.status, SubtaskTraceStatus):
            self.status = SubtaskTraceStatus(str(self.status or SubtaskTraceStatus.RUNNING.value))

    def add_message(self, message: Message) -> None:
        payload = message.to_dict()
        payload.pop("state_snapshot", None)
        self.messages.append(payload)
        if message.role == "assistant" and message.tool_calls:
            self.tool_count += len(message.tool_calls or [])
        if message.role == "assistant" and str(message.content or "").strip():
            self.final_message = str(message.content or "").strip()

    def finish(
        self,
        status: SubtaskTraceStatus,
        *,
        final_message: str = "",
        error: str = "",
    ) -> None:
        self.status = status
        self.finished_at = datetime.now().isoformat(timespec="seconds")
        if final_message:
            self.final_message = final_message
        if error:
            self.error = error
        try:
            started = datetime.fromisoformat(self.started_at)
            finished = datetime.fromisoformat(self.finished_at)
            self.duration_ms = max(0, int((finished - started).total_seconds() * 1000))
        except Exception:
            self.duration_ms = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "title": self.title,
            "goal": self.goal,
            "status": self.status.value if isinstance(self.status, SubtaskTraceStatus) else str(self.status),
            "mode": self.mode,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": int(self.duration_ms or 0),
            "messages": [dict(item) for item in self.messages if isinstance(item, dict)],
            "final_message": self.final_message,
            "error": self.error,
            "tool_count": int(self.tool_count or 0),
            "token_count": int(self.token_count or 0),
            "depth": int(self.depth or 0),
            "metadata": dict(self.metadata or {}),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SubtaskTrace":
        payload = data if isinstance(data, dict) else {}
        return cls(
            id=str(payload.get("id") or ""),
            kind=str(payload.get("kind") or "subagent"),
            name=str(payload.get("name") or "subtask"),
            title=str(payload.get("title") or "Subtask"),
            goal=str(payload.get("goal") or ""),
            status=SubtaskTraceStatus(str(payload.get("status") or "running")),
            mode=str(payload.get("mode") or "agent"),
            started_at=str(payload.get("started_at") or ""),
            finished_at=str(payload.get("finished_at") or ""),
            duration_ms=int(payload.get("duration_ms") or 0),
            messages=[dict(item) for item in payload.get("messages") or [] if isinstance(item, dict)],
            final_message=str(payload.get("final_message") or ""),
            error=str(payload.get("error") or ""),
            tool_count=int(payload.get("tool_count") or 0),
            token_count=int(payload.get("token_count") or 0),
            depth=int(payload.get("depth") or 0),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass
class SubTaskOutcome:
    status: TaskStatus
    message: str
    completion_command: str = ""
    completed: bool = False
    trace: Optional[SubtaskTrace] = None


# ---------------------------------------------------------------------------
# TaskEvent — lightweight envelope emitted during execution
# ---------------------------------------------------------------------------
class TaskEventKind(str, Enum):
    TURN_START = "turn_start"
    TOKEN = "token"
    THINKING = "thinking"
    STEP = "step"           # assistant / tool message committed
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    RETRY = "retry"         # about to retry after error
    CONDENSE = "condense"   # context condensed
    COMPLETE = "complete"
    ERROR = "error"


@dataclass
class TaskEvent:
    kind: TaskEventKind
    data: Any = None          # Message, str, dict …
    turn: int = 0
    detail: str = ""
    source: str = "parent"
    subtask_id: str = ""
    parent_message_id: str = ""
    parent_tool_call_id: str = ""
    root_tool_call_id: str = ""


class TaskTurnState(str, Enum):
    TURN_START = "turn_start"
    PRE_TURN_HOOKS = "pre_turn_hooks"
    CONDENSING = "condensing"
    LLM_CALL = "llm_call"
    ASSISTANT_RECEIVED = "assistant_received"
    TOOL_EXECUTION = "tool_execution"
    TURN_COMPLETE = "turn_complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TurnOutcomeKind(str, Enum):
    CONTINUE = "continue"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TurnContext:
    turn: int
    nudge_count: int = 0
    runtime_messages: list[Message] = field(default_factory=list)
    state: TaskTurnState = TaskTurnState.TURN_START


@dataclass
class TurnOutcome:
    kind: TurnOutcomeKind
    context: TurnContext
    final_message: Optional[Message] = None
    error: Optional[str] = None
    next_policy: Optional["RunPolicy"] = None


# ---------------------------------------------------------------------------
# RetryPolicy
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RetryPolicy:
    """Controls automatic retry behaviour on transient LLM errors."""

    max_retries: int = 3
    base_delay: float = 1.0     # seconds
    max_delay: float = 60.0
    backoff_factor: float = 2.0


# ---------------------------------------------------------------------------
# RunPolicy  – replaces core.agent.policy.RunPolicy
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RunPolicy:
    """Tiny, immutable policy object driving the task loop.

    All chat/agent/code/… differences are purely parameter-driven.
    Tool visibility and auto-approval are controlled by effective tool
    permissions: per-tool overrides first, then category defaults.
    """

    mode: str = "chat"
    max_turns: int = 200
    context_window_limit: int = 100_000

    enable_thinking: bool = True
    tool_selection: ToolSelectionPolicy = field(default_factory=ToolSelectionPolicy.all)
    tool_permissions: ToolPermissionConfig = field(default_factory=ToolPermissionConfig)

    # Model / generation overrides (None → use provider defaults).
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None

    # Retry policy for transient LLM errors.
    retry: RetryPolicy = field(default_factory=RetryPolicy)

    # None → always compress (unless app config disables).
    auto_compress_enabled: Optional[bool] = None

    # Source of the task execution: desktop, channel, sub_task, system.
    # Used by the permission layer to apply source-aware policies.
    source: str = "desktop"
