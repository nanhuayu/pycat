"""Multi-agent coordination tools.

``new_task``
    Orchestrator delegates a sub-task to a specific mode.  The sub-task
    runs in an independent conversation context and returns its result
    via ``attempt_completion``.

``attempt_completion``
    Called by the agent to signal that the current sub-task is done.
    In a top-level conversation this is simply informational; in a
    sub-task it terminates the child Task and returns the result to the
    parent.
"""
from __future__ import annotations

from typing import Any, Dict

from core.tools.base import BaseTool, ToolContext, ToolResult


class NewTaskTool(BaseTool):
    """Delegate a sub-task to a different mode."""

    @property
    def name(self) -> str:
        return "new_task"

    @property
    def description(self) -> str:
        return (
            "Create a new sub-task delegated to a different mode/agent. "
            "The sub-task receives its own conversation context. "
            "Use this when the current mode is not suited for a particular "
            "operation (e.g. orchestrator delegates coding to 'code' mode). "
            "The sub-task result will be returned when it completes."
        )

    @property
    def group(self) -> str:
        return "modes"

    @property
    def category(self) -> str:
        return "misc"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "description": "Mode slug for the sub-task (e.g. 'code', 'plan', 'debug')",
                },
                "message": {
                    "type": "string",
                    "description": "The task description / instructions for the sub-agent",
                },
            },
            "required": ["mode", "message"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        mode = (arguments.get("mode") or "").strip()
        message = (arguments.get("message") or "").strip()

        if not mode or not message:
            return ToolResult("Both 'mode' and 'message' are required.", is_error=True)

        # The actual sub-task execution is handled by the Task engine.
        # This tool merely signals the intent — the Task.run() loop
        # detects a new_task tool call and spawns a child Task.
        #
        # For now, we store the sub-task request in context.state so
        # the Task engine can detect it after tool execution.
        context.state["_pending_subtask"] = {
            "mode": mode,
            "message": message,
        }

        return ToolResult(
            f"Sub-task created for mode '{mode}'. "
            f"The task will run with its own context and return results when complete."
        )


class AttemptCompletionTool(BaseTool):
    """Signal that the current task is complete."""

    @property
    def name(self) -> str:
        return "attempt_completion"

    @property
    def description(self) -> str:
        return (
            "Signal that you have completed the current task. "
            "Provide a result summary. In a sub-task, this returns "
            "the result to the parent agent."
        )

    @property
    def group(self) -> str:
        return "modes"

    @property
    def category(self) -> str:
        return "misc"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "result": {
                    "type": "string",
                    "description": "Summary of the completed work",
                },
                "command": {
                    "type": "string",
                    "description": "Optional command to demonstrate the result (e.g. 'open browser')",
                },
            },
            "required": ["result"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        result = (arguments.get("result") or "").strip()
        command = (arguments.get("command") or "").strip()

        if not result:
            return ToolResult("Missing 'result'.", is_error=True)

        # Signal completion through context state
        context.state["_task_completed"] = True
        context.state["_completion_result"] = result
        if command:
            context.state["_completion_command"] = command

        return ToolResult("Completion acknowledged.")


class SwitchModeTool(BaseTool):
    """Switch the current conversation to a different mode."""

    @property
    def name(self) -> str:
        return "switch_mode"

    @property
    def description(self) -> str:
        return (
            "Switch the current conversation to a different mode. "
            "Use when the user's request is better suited for another mode."
        )

    @property
    def group(self) -> str:
        return "modes"

    @property
    def category(self) -> str:
        return "misc"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "description": "Target mode slug (e.g. 'code', 'plan', 'ask')",
                },
                "reason": {
                    "type": "string",
                    "description": "Why the mode switch is needed",
                },
            },
            "required": ["mode"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        mode = (arguments.get("mode") or "").strip()
        reason = (arguments.get("reason") or "").strip()

        if not mode:
            return ToolResult("Missing 'mode'.", is_error=True)

        context.state["_mode_switch"] = mode
        msg = f"Mode switched to '{mode}'."
        if reason:
            msg += f" Reason: {reason}"
        return ToolResult(msg)
