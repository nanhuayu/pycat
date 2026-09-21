"""Unified agent run data model.

A root task, a delegated subagent, and a capability child run are all represented
as an ``AgentRunContext``. Execution code should depend on this node context
instead of branching on "parent vs subtask". Nesting is represented only by the
optional parent link and trace.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pycat.models.conversation import Conversation
from pycat.models.provider import Provider
from pycat.models.contracts.agent import RunPolicy, SubtaskTrace, RunResult


@dataclass(frozen=True)
class AgentRunParent:
    run_id: str = ""
    message_id: str = ""
    tool_call_id: str = ""
    root_tool_call_id: str = ""


@dataclass
class AgentRunContext:
    id: str
    kind: str
    mode: str
    depth: int
    conversation: Conversation
    provider: Provider
    policy: RunPolicy
    parent: AgentRunParent | None = None
    trace: SubtaskTrace | None = None
    metadata: dict[str, Any] | None = None

    @property
    def is_nested(self) -> bool:
        return self.parent is not None or self.depth > 0

    @property
    def parent_tool_call_id(self) -> str:
        return self.parent.tool_call_id if self.parent is not None else ""


@dataclass
class AgentRunResult:
    context: AgentRunContext
    task_result: RunResult
    completed: bool = False

    @property
    def message(self) -> str:
        if self.task_result.final_message is not None:
            return str(self.task_result.final_message.content or "")
        if self.task_result.error:
            return str(self.task_result.error)
        return ""
