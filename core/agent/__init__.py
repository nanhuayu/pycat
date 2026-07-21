"""Agent orchestration domain."""

from __future__ import annotations

from typing import Any

__all__ = ["AgentRuntime", "AgentRunEngine"]


def __getattr__(name: str) -> Any:
    if name == "AgentRuntime":
        from core.agent.run.runtime import AgentRuntime

        return AgentRuntime
    if name == "AgentRunEngine":
        from core.agent.run.engine import AgentRunEngine

        return AgentRunEngine
    raise AttributeError(name)
