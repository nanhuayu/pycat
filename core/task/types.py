"""Data types for the task execution engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional, Set

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


@dataclass
class SubTaskOutcome:
    status: TaskStatus
    message: str
    completion_command: str = ""
    completed: bool = False


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
    """

    mode: str = "chat"
    max_turns: int = 20
    context_window_limit: int = 100_000

    enable_thinking: bool = True
    enable_search: bool = False
    enable_mcp: bool = False

    # Model / generation overrides (None → use provider defaults).
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None

    # If set, only these tools are callable.
    tool_allowlist: Optional[Set[str]] = None
    tool_denylist: Optional[Set[str]] = None

    # Retry policy for transient LLM errors.
    retry: RetryPolicy = field(default_factory=RetryPolicy)

    # None → always compress (unless app config disables).
    auto_compress_enabled: Optional[bool] = None
