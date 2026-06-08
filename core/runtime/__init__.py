"""Runtime package exports.

Keep this module lightweight: many callers import ``core.runtime.events`` during
test collection and UI startup. Heavy runtime objects are loaded lazily to avoid
pulling ``Task`` and ``ToolManager`` into unrelated imports.
"""
from __future__ import annotations

from core.runtime.events import TurnEvent, TurnEventKind

__all__ = [
    "AgentRuntime",
    "RuntimeCallContext",
    "RuntimeTextResult",
    "TurnEvent",
    "TurnEventKind",
    "TurnEngine",
    "RuntimePolicyFactory",
]


def __getattr__(name: str):
    if name in {"AgentRuntime", "RuntimeCallContext", "RuntimeTextResult"}:
        from core.runtime.agent_runtime import AgentRuntime, RuntimeCallContext, RuntimeTextResult

        exports = {
            "AgentRuntime": AgentRuntime,
            "RuntimeCallContext": RuntimeCallContext,
            "RuntimeTextResult": RuntimeTextResult,
        }
        return exports[name]
    if name == "TurnEngine":
        from core.runtime.turn_engine import TurnEngine

        return TurnEngine
    if name == "RuntimePolicyFactory":
        from core.runtime.policy_factory import RuntimePolicyFactory

        return RuntimePolicyFactory
    raise AttributeError(name)
