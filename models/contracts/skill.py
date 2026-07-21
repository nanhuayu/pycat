"""Skill system contracts."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from models.contracts.tooling import ToolSelectionPolicy


@dataclass
class Skill:
    """Loaded skill definition."""

    name: str
    content: str
    source: str
    description: str = ""
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    source_scope: str = "project"
    read_only: bool = False


@dataclass(frozen=True)
class SkillInvocationSpec:
    """Declared runtime contract for an explicit skill run."""

    mode: str = "agent"
    executor: str = "instruction"
    execution_mode: str = "inline"
    user_invocable: bool = True
    disable_model_invocation: bool = False
    tool_selection: ToolSelectionPolicy = field(default_factory=ToolSelectionPolicy.all)
    declared_tools: Tuple[str, ...] = ()
    preferred_cli: Tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillExecutionCheck:
    """Whether a skill can execute with the currently exposed concrete tools."""

    executable: bool
    concrete_tools: Tuple[str, ...] = ()
    reason: str = ""
    missing_tools: Tuple[str, ...] = ()
