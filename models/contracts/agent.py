"""Agent run contracts shared by runtime, UI, CLI, and channel gateways."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional, TYPE_CHECKING

from models.contracts.tooling import ToolPermissionConfig, ToolSelectionPolicy
from models.conversation import Message

if TYPE_CHECKING:
    from models.conversation import Conversation


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    FAILED = "failed"


class RunStopReason(str, Enum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"
    OUTPUT_LIMIT = "output_limit"
    INCOMPLETE_RESPONSE = "incomplete_response"
    EMPTY_RESPONSE = "empty_response"
    MAX_TURNS = "max_turns"
    EXPLICIT_COMPLETION_MISSING = "explicit_completion_missing"
    TOOL_DISABLED = "tool_disabled"
    PERMISSION_DENIED = "permission_denied"


@dataclass
class RunResult:
    status: RunStatus = RunStatus.COMPLETED
    final_message: Optional[Message] = None
    error: Optional[str] = None
    stop_reason: RunStopReason = RunStopReason.COMPLETED
    conversation: Optional["Conversation"] = None


class SubtaskTraceStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class SubtaskTrace:
    """Persistent, renderable transcript for one delegated child run."""

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


class RunEventKind(str, Enum):
    TURN_START = "turn_start"
    STEP = "step"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    RETRY = "retry"
    CONDENSE = "condense"
    COMPLETE = "complete"
    ERROR = "error"


@dataclass
class RunEvent:
    kind: RunEventKind
    data: Any = None
    turn: int = 0
    detail: str = ""
    source: str = "parent"
    subtask_id: str = ""
    parent_message_id: str = ""
    parent_tool_call_id: str = ""
    root_tool_call_id: str = ""

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


class TurnState(str, Enum):
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
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TurnContext:
    turn: int
    had_tool_work: bool = False
    incomplete_responses: int = 0
    runtime_messages: list[Message] = field(default_factory=list)
    memory_advice: str = ""
    state: TurnState = TurnState.TURN_START


@dataclass
class TurnOutcome:
    kind: TurnOutcomeKind
    context: TurnContext
    final_message: Optional[Message] = None
    error: Optional[str] = None
    stop_reason: RunStopReason = RunStopReason.COMPLETED


_FALSE_BOOLEAN_VALUES = frozenset({"0", "false", "no", "off", "disabled", "disable"})


def effective_pycat_assistant_enabled(value: object = True, *, mode: str = "chat") -> bool:
    """Resolve the session prompt-layer switch for one run.

    The switch is intentionally a Chat-only control. Agent-like modes retain
    their runtime instructions even when a stale or shared setting says false.
    Keeping this pure and in the contract layer lets policy construction and
    prompt rendering agree without introducing a second settings owner.
    """
    if str(mode or "chat").strip().lower() != "chat":
        return True
    if isinstance(value, bool):
        return value
    if value is None:
        return True
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in _FALSE_BOOLEAN_VALUES


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    backoff_factor: float = 2.0


@dataclass(frozen=True)
class RunPolicy:
    """Immutable policy object driving one agent run."""

    mode: str = "chat"
    max_turns: int = 200

    reasoning_mode: str | None = None
    show_thinking: bool = True
    pycat_assistant_enabled: bool = True
    completion_policy: str = "text"
    tool_selection: ToolSelectionPolicy = field(default_factory=ToolSelectionPolicy.all)
    tool_permissions: ToolPermissionConfig = field(default_factory=ToolPermissionConfig)

    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    completion_schema: Optional[dict[str, Any]] = None

    retry: RetryPolicy = field(default_factory=RetryPolicy)
    auto_compress_enabled: Optional[bool] = None
    source: str = "desktop"
