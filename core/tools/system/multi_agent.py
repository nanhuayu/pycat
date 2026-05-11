"""Default sub-agent coordination tools.

``subagent__read_analyze``
    Read-only subagent for multi-file, long-document, and cross-source
    analysis. Fixed tool chain: read + modes.

``subagent__search``
    Search-focused subagent for research and fact-checking. Fixed tool chain:
    search + read + modes.

``subagent__custom``
    General-purpose default subagent with a custom goal. The parent agent may
    narrow the child tool categories for focused delegated work.

``attempt_completion``
    Called by the agent to signal that the current sub-task is done.

``switch_mode``
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


# ---------------------------------------------------------------------------
# Dedicated subagents (fixed tool chains)
# ---------------------------------------------------------------------------

class SubagentReadAnalyzeTool(BaseTool):
    """Read-only subagent for analyzing files and long text.

    The subagent is restricted to read + modes tools. It cannot write files,
    execute commands, or access the network.
    """

    @property
    def name(self) -> str:
        return "subagent__read_analyze"

    @property
    def description(self) -> str:
        return (
            "Launch a read-only subagent for multi-file or cross-source long-text analysis. "
            "Use this for summarizing several files, comparing documents, extracting evidence, "
            "or producing a structured report from content that does not fit in the parent context. "
            "Routing: one file or one long text -> capability__summarize_text; multi-file synthesis -> subagent__read_analyze. "
            "This subagent has read-only access and cannot modify files or run commands."
        )


    @property
    def category(self) -> str:
        return "delegate"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "What to analyze or summarize. Be specific about the desired outcome.",
                },
                "path": {
                    "type": "string",
                    "description": "Path to the file to read and analyze (optional).",
                },
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of multiple files to read and analyze together.",
                },
                "input_text": {
                    "type": "string",
                    "description": "Direct text content to analyze (optional). Use this when the content is already in the conversation.",
                },
                "output_format": {
                    "type": "string",
                    "description": "Desired output format, e.g. 'summary', 'bullet points', 'structured report', 'extracted key facts'.",
                },
                "max_length": {
                    "type": "number",
                    "description": "Maximum output length in characters.",
                },
                "focus": {
                    "type": "string",
                    "description": "Specific aspect to focus on, e.g. 'key arguments', 'risks', 'architecture', 'dependencies'.",
                },
            },
            "required": ["goal"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        goal = str(arguments.get("goal") or "").strip()
        if not goal:
            return ToolResult("Missing 'goal' for subagent__read_analyze.", is_error=True)

        path = str(arguments.get("path") or "").strip()
        paths = normalize_string_list(arguments.get("paths"))
        input_text = str(arguments.get("input_text") or "").strip()
        output_format = str(arguments.get("output_format") or "").strip() or "structured report"
        focus = str(arguments.get("focus") or "").strip()
        max_length = arguments.get("max_length")

        parts: list[str] = [f"Goal: {goal}"]
        if path:
            parts.append(f"File path: {path}")
        if paths:
            parts.append("File paths:\n" + "\n".join(f"- {path}" for path in paths))
        if input_text:
            parts.append(f"Content to analyze:\n{input_text}")
        if focus:
            parts.append(f"Focus: {focus}")
        if max_length not in (None, ""):
            try:
                parts.append(f"Maximum output length: {int(max_length)} characters")
            except Exception:
                parts.append(f"Maximum output length: {max_length}")

        message = build_capability_subtask_message(
            task=goal,
            input_text="\n\n".join(parts),
            output_format=output_format,
            instructions=(
                "You are a read-only analysis assistant. "
                "You may read files and analyze text, but you must NOT write files, "
                "execute commands, or access external networks. "
                "Return a concise, structured report to the parent agent."
            ),
        )

        schedule_subtask(
            context,
            mode="agent",
            message=message,
            title="Read/analyze",
            kind="subagent",
            allowed_tool_categories=["read", "manage"],
            instructions=(
                "You are a read-only analysis assistant. "
                "You may read files and analyze text, but you must NOT write files, "
                "execute commands, or access external networks. "
                "Return a concise, structured report to the parent agent."
            ),
        )

        return ToolResult(
            "Read-analyze subagent scheduled. It will read the specified content and return a structured report."
        )


class SubagentExploreTool(BaseTool):
    """Read-only subagent for broad-to-narrow workspace exploration."""

    @property
    def name(self) -> str:
        return "subagent__explore"

    @property
    def description(self) -> str:
        return (
            "Launch a read-only exploration subagent for codebase discovery and architecture Q&A. "
            "Use this when you need fast broad-to-narrow searches, relevant files/symbols/patterns, "
            "or implementation risks before planning or editing. The subagent cannot modify files or run commands."
        )


    @property
    def category(self) -> str:
        return "delegate"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "Exploration goal. Be specific about the area, behavior, or architecture question.",
                },
                "focus": {
                    "type": "string",
                    "description": "Optional focus such as files, symbols, risks, dependencies, or test coverage.",
                },
                "thoroughness": {
                    "type": "string",
                    "enum": ["quick", "medium", "thorough"],
                    "description": "How deep the exploration should be. Default: medium.",
                },
                "output_format": {
                    "type": "string",
                    "description": "Desired format, e.g. findings with file paths, implementation map, or risk report.",
                },
            },
            "required": ["goal"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        goal = str(arguments.get("goal") or "").strip()
        if not goal:
            return ToolResult("Missing 'goal' for subagent__explore.", is_error=True)

        focus = str(arguments.get("focus") or "").strip()
        thoroughness = str(arguments.get("thoroughness") or "medium").strip().lower() or "medium"
        if thoroughness not in {"quick", "medium", "thorough"}:
            thoroughness = "medium"
        output_format = str(arguments.get("output_format") or "").strip() or "evidence-backed findings with file paths and symbols"

        parts = [f"Exploration goal: {goal}", f"Thoroughness: {thoroughness}"]
        if focus:
            parts.append(f"Focus: {focus}")

        instructions = (
            "You are a read-only codebase explorer. Start broad, then narrow to the most relevant files and symbols. "
            "Use search and file reads only; do NOT write files, execute commands, or mutate state. "
            "Return concrete file paths, symbols, patterns, risks, and open questions. "
            "Do not create todos, memory, or artifacts unless the parent explicitly requested it."
        )
        message = build_capability_subtask_message(
            task=goal,
            input_text="\n\n".join(parts),
            output_format=output_format,
            instructions=instructions,
        )

        schedule_subtask(
            context,
            mode="explore",
            message=message,
            title="Explore",
            kind="subagent",
            allowed_tool_categories=["read", "search", "manage"],
            instructions=instructions,
        )

        return ToolResult("Explore subagent scheduled. It will inspect the workspace read-only and return evidence-backed findings.")


class SubagentSearchTool(BaseTool):
    """Search-focused subagent for research and fact-checking.

    The subagent is restricted to search + read + modes tools.
    """

    @property
    def name(self) -> str:
        return "subagent__search"

    @property
    def description(self) -> str:
        return (
            "Launch a search-focused subagent to find and synthesize information. "
            "Use this for research, fact-checking, or gathering external context. "
            "Routing: targeted single-topic research -> capability__summarize_text or subagent__search; broader multi-source research -> subagent__search. "
            "The subagent has search and read access, but cannot modify files or run commands."
        )


    @property
    def category(self) -> str:
        return "delegate"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "Research goal or question. Be specific.",
                },
                "query": {
                    "type": "string",
                    "description": "Initial search query (optional). The subagent may refine this.",
                },
                "output_format": {
                    "type": "string",
                    "description": "Desired output format, e.g. 'research brief', 'bullet points', 'structured findings'.",
                },
                "max_turns": {
                    "type": "number",
                    "description": "Maximum number of search iterations. Default: 10.",
                },
            },
            "required": ["goal"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        goal = str(arguments.get("goal") or "").strip()
        if not goal:
            return ToolResult("Missing 'goal' for subagent__search.", is_error=True)

        query = str(arguments.get("query") or "").strip()
        output_format = str(arguments.get("output_format") or "").strip() or "research brief"
        max_turns = self._parse_max_turns(arguments.get("max_turns"), default_turns=10)

        parts: list[str] = [f"Research goal: {goal}"]
        if query:
            parts.append(f"Initial query: {query}")

        message = build_capability_subtask_message(
            task=goal,
            input_text="\n\n".join(parts),
            output_format=output_format,
            instructions=(
                "You are a research assistant. "
                "Use web_search directly for web research and read_file/search_content for local evidence. "
                "Do not delegate again to subagent__search unless the parent explicitly asks for nested delegation. "
                "but you must NOT write files, execute commands, or modify the workspace. "
                "Cite sources where possible. Return a concise, structured report to the parent agent."
            ),
        )

        schedule_subtask(
            context,
            mode="agent",
            message=message,
            title="Search",
            kind="subagent",
            max_turns=max_turns,
            allowed_tool_categories=["search", "read", "manage"],
            instructions=(
                "You are a research assistant. "
                "Use web_search directly for web research and read_file/search_content for local evidence. "
                "Do not delegate again to subagent__search unless the parent explicitly asks for nested delegation. "
                "but you must NOT write files, execute commands, or modify the workspace. "
                "Cite sources where possible. Return a concise, structured report to the parent agent."
            ),
        )

        return ToolResult(
            "Search subagent scheduled. It will search for information and return synthesized findings."
        )

    @staticmethod
    def _parse_max_turns(value: Any, default_turns: int = 10) -> int | None:
        try:
            turns = int(value) if value not in (None, "") else 0
        except Exception:
            turns = 0
        return turns if turns > 0 else default_turns


# ---------------------------------------------------------------------------
# General-purpose custom subagent
# ---------------------------------------------------------------------------

class SubagentCustomTool(BaseTool):
    """Launch a general-purpose subagent with a custom goal.

    Unlike the dedicated read_analyze and search subagents, the parent agent
    decides which tools the subagent can use via request-time tool selection.
    """

    @property
    def name(self) -> str:
        return "subagent__custom"

    @property
    def description(self) -> str:
        return (
            "Launch a general-purpose subagent with a custom goal. "
            "Use this when the task requires tools beyond reading and searching, "
            "such as editing files, running commands, or using MCP tools. "
            "The parent agent must explicitly narrow allowed tool categories and expected output. "
            "Prefer capability__* tools for standard tasks (translation, summary, title extract, etc.)."
        )


    @property
    def category(self) -> str:
        return "delegate"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "The task goal. Be specific about what the subagent should accomplish.",
                },
                "instructions": {
                    "type": "string",
                    "description": "Detailed instructions for the subagent, including constraints and expected output format.",
                },
                "allowed_tool_categories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Allowed tool categories for the subagent. "
                        "Default: ['read', 'manage']. "
                        "Common categories: read, search, edit, execute, manage, delegate, extension, mcp."
                    ),
                },
                "model": {
                    "type": "string",
                    "description": "Optional model override in provider|model form.",
                },
                "max_turns": {
                    "type": "number",
                    "description": "Optional maximum turns for the child agent. Default: 20.",
                },
            },
            "required": ["goal"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        goal = str(arguments.get("goal") or "").strip()
        if not goal:
            return ToolResult("Missing 'goal' for subagent__custom.", is_error=True)

        instructions = str(arguments.get("instructions") or "").strip()
        model_ref = str(arguments.get("model") or "").strip()
        max_turns = self._parse_max_turns(arguments.get("max_turns"))

        allowed_tool_categories = normalize_string_list(arguments.get("allowed_tool_categories"))
        if not allowed_tool_categories:
            allowed_tool_categories = ["read", "manage"]
        else:
            allowed_tool_categories = [g for g in allowed_tool_categories if g]
            if "manage" not in allowed_tool_categories:
                allowed_tool_categories.append("manage")

        input_parts: list[str] = [f"Goal: {goal}"]
        if instructions:
            input_parts.append(f"Instructions:\n{instructions}")

        message = build_capability_subtask_message(
            task=goal,
            input_text="\n\n".join(input_parts),
            instructions=instructions,
        )

        schedule_subtask(
            context,
            mode="agent",
            message=message,
            title="Custom",
            kind="subagent",
            model_ref=model_ref,
            max_turns=max_turns,
            allowed_tool_categories=allowed_tool_categories,
            instructions=instructions,
        )

        return ToolResult(
            f"Custom subagent scheduled with allowed tool categories: {', '.join(allowed_tool_categories)}. "
            "Its result will be returned to the parent agent."
        )

    @staticmethod
    def _parse_max_turns(value: Any) -> int | None:
        try:
            turns = int(value) if value not in (None, "") else 0
        except Exception:
            turns = 0
        return turns if turns > 0 else None


# ---------------------------------------------------------------------------
# Control tools
# ---------------------------------------------------------------------------

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
        return "switch_mode"

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
