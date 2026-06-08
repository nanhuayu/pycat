"""Default sub-agent coordination tools.

``agent__run``
    Unified nested-agent entry point. The parent agent chooses an ``agent_id``
    such as ``explore``, ``search``, or ``read_analyze`` and the runtime applies
    that agent profile's tool and permission boundary.

``agent__complete``
    Called by the agent to signal that the current sub-task is done.

``agent__switch``
    Switch the current conversation to a different mode.
"""
from __future__ import annotations

from typing import Any, Dict

from core.tools.base import BaseTool, ToolContext, ToolResult
from core.tools.system.capability_tools import (
    build_capability_subtask_message,
    normalize_string_list,
    schedule_subtask,
)


_NESTED_AGENT_TOOL_CATEGORIES: dict[str, list[str]] = {
    "explore": ["read", "search", "manage"],
    "search": ["search", "read", "manage"],
    "read_analyze": ["read", "manage"],
    "plan": ["read", "search", "manage"],
    "review": ["read", "search", "manage"],
}


class AgentRunTool(BaseTool):
    """Run a nested agent with a focused goal."""

    @property
    def name(self) -> str:
        return "agent__run"

    @property
    def description(self) -> str:
        return (
            "Run a focused nested agent and return its structured result. "
            "Use explore for read-only codebase discovery, search for multi-source research/fact-checking, "
            "and read_analyze for multi-file or long-document synthesis. "
            "Nested agents do not inherit parent write permissions by default."
        )

    @property
    def category(self) -> str:
        return "delegate"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "enum": ["explore", "search", "read_analyze", "plan", "review"],
                    "description": "Nested agent profile to run.",
                },
                "goal": {
                    "type": "string",
                    "description": "Focused goal for the nested agent.",
                },
                "input_text": {
                    "type": "string",
                    "description": "Direct text or context to analyze.",
                },
                "path": {
                    "type": "string",
                    "description": "Optional single workspace path relevant to the goal.",
                },
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of workspace paths relevant to the goal.",
                },
                "query": {
                    "type": "string",
                    "description": "Initial search query for search agents.",
                },
                "focus": {
                    "type": "string",
                    "description": "Specific aspect to focus on.",
                },
                "output_format": {
                    "type": "string",
                    "description": "Desired output format.",
                },
                "instructions": {
                    "type": "string",
                    "description": "Additional constraints or instructions for this run.",
                },
                "model": {
                    "type": "string",
                    "description": "Optional model override in provider|model form.",
                },
                "max_turns": {
                    "type": "number",
                    "description": "Optional maximum turns for the nested agent.",
                },
            },
            "required": ["agent_id", "goal"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        agent_id = str(arguments.get("agent_id") or "").strip().lower()
        if agent_id not in _NESTED_AGENT_TOOL_CATEGORIES:
            return ToolResult(f"Unsupported nested agent_id: {agent_id or '<empty>'}", is_error=True)

        goal = str(arguments.get("goal") or "").strip()
        if not goal:
            return ToolResult("Missing 'goal' for agent__run.", is_error=True)

        input_parts = [f"Goal: {goal}"]
        path = str(arguments.get("path") or "").strip()
        paths = normalize_string_list(arguments.get("paths"))
        input_text = str(arguments.get("input_text") or "").strip()
        query = str(arguments.get("query") or "").strip()
        focus = str(arguments.get("focus") or "").strip()
        output_format = str(arguments.get("output_format") or "").strip() or "structured report"
        instructions = str(arguments.get("instructions") or "").strip()
        model_ref = str(arguments.get("model") or "").strip()
        max_turns = self._parse_max_turns(arguments.get("max_turns"))

        if path:
            input_parts.append(f"Path: {path}")
        if paths:
            input_parts.append("Paths:\n" + "\n".join(f"- {item}" for item in paths))
        if query:
            input_parts.append(f"Initial query: {query}")
        if input_text:
            input_parts.append(f"Input:\n{input_text}")
        if focus:
            input_parts.append(f"Focus: {focus}")

        default_instructions = self._instructions_for(agent_id)
        combined_instructions = "\n\n".join(part for part in (default_instructions, instructions) if part)
        message = build_capability_subtask_message(
            task=goal,
            input_text="\n\n".join(input_parts),
            output_format=output_format,
            instructions=combined_instructions,
        )

        action = schedule_subtask(
            context,
            mode=agent_id,
            message=message,
            title=agent_id.replace("_", " ").title(),
            kind="agent",
            model_ref=model_ref,
            max_turns=max_turns,
            allowed_tool_categories=_NESTED_AGENT_TOOL_CATEGORIES[agent_id],
            instructions=combined_instructions,
        )

        return ToolResult(
            f"Nested agent '{agent_id}' scheduled. Its result will be returned to the parent agent.",
            control_action=action,
        )

    @staticmethod
    def _parse_max_turns(value: Any) -> int | None:
        try:
            turns = int(value) if value not in (None, "") else 0
        except Exception:
            turns = 0
        return turns if turns > 0 else None

    @staticmethod
    def _instructions_for(agent_id: str) -> str:
        if agent_id == "explore":
            return (
                "You are a read-only codebase explorer. Use search and file reads only. "
                "Return concrete file paths, symbols, patterns, risks, and open questions."
            )
        if agent_id == "search":
            return (
                "You are a research and fact-checking agent. Use web__search/web__fetch and local read/search tools. "
                "Do not modify the workspace. Cite sources where possible."
            )
        if agent_id == "read_analyze":
            return (
                "You are a read-only analysis agent for multi-file or long-document synthesis. "
                "Compare evidence and return a concise structured report."
            )
        if agent_id == "plan":
            return "You are a planning agent. Gather context and produce a clear plan without making edits."
        if agent_id == "review":
            return "You are a review agent. Inspect evidence, risks, and quality issues without making edits."
        return ""


# ---------------------------------------------------------------------------
# Control tools
# ---------------------------------------------------------------------------

class AttemptCompletionTool(BaseTool):
    """Signal that the current task is complete."""

    @property
    def name(self) -> str:
        return "agent__complete"

    @property
    def description(self) -> str:
        return (
            "Signal that you have completed the current task. "
            "Provide a result summary. In a sub-task, this returns "
            "the result to the parent agent."
        )


    @property
    def category(self) -> str:
        return "manage"

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

        context.state["_task_completed"] = True
        context.state["_completion_result"] = result
        if command:
            context.state["_completion_command"] = command

        return ToolResult("Completion acknowledged.")


class SwitchModeTool(BaseTool):
    """Switch the current conversation to a different mode."""

    @property
    def name(self) -> str:
        return "agent__switch"

    @property
    def description(self) -> str:
        return (
            "Switch the current conversation to a different mode. "
            "Use when the user's request is better suited for another mode."
        )


    @property
    def category(self) -> str:
        return "manage"

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
