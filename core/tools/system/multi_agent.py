"""Delegated-agent tool backed by workspace Mode profiles."""
from __future__ import annotations

from typing import Any, Dict

from core.modes.manager import ModeManager
from core.tools.base import BaseTool, ToolContext, ToolControlAction, ToolResult
from models.contracts.tooling import ToolSelectionPolicy


class AgentRunTool(BaseTool):
    @property
    def name(self) -> str:
        return "agent__run"

    @property
    def display_name(self) -> str:
        return "Run sub-agent"

    @property
    def description(self) -> str:
        return "Run one configured sub-agent profile for a focused goal; its permissions can only narrow the parent run."

    @property
    def category(self) -> str:
        return "delegate"

    @property
    def risk(self) -> str:
        return "medium"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return self.input_schema_for_work_dir(".")

    @staticmethod
    def profile_names(work_dir: str) -> list[str]:
        try:
            return [profile.slug for profile in ModeManager(work_dir).list_subagent_profiles()]
        except Exception:
            return []

    @classmethod
    def input_schema_for_work_dir(cls, work_dir: str) -> Dict[str, Any]:
        profile_schema: dict[str, Any] = {
            "type": "string",
            "description": "Configured sub-agent profile.",
        }
        profiles = cls.profile_names(work_dir)
        if profiles:
            profile_schema["enum"] = profiles
        return {
            "type": "object",
            "properties": {
                "profile": profile_schema,
                "goal": {"type": "string", "description": "Focused outcome for the sub-agent."},
                "refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional artifact ids, archive ids, paths, or URLs.",
                },
                "instructions": {"type": "string", "description": "Optional run-specific constraints."},
            },
            "required": ["profile", "goal"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        profile_name = str(arguments.get("profile") or "").strip().lower()
        profiles = {item.slug: item for item in ModeManager(context.work_dir).list_subagent_profiles()}
        profile = profiles.get(profile_name)
        if profile is None:
            available = ", ".join(sorted(profiles)) or "none"
            return ToolResult(f"Unknown sub-agent profile '{profile_name}'. Available: {available}.", is_error=True)

        goal = str(arguments.get("goal") or "").strip()
        if not goal:
            return ToolResult("goal is required.", is_error=True)

        refs: list[str] = []
        for item in arguments.get("refs") or []:
            value = str(item or "").strip()
            if value and value not in refs:
                refs.append(value)
        instructions = str(arguments.get("instructions") or "").strip()

        message_parts = [goal]
        if refs:
            message_parts.append("References:\n" + "\n".join(f"- {item}" for item in refs))
        payload = {
            "mode": profile.slug,
            "message": "\n\n".join(message_parts),
            "title": profile.name,
            "kind": "agent",
            "instructions": instructions,
            "context_refs": refs,
            "shared_context_policy": profile.shared_context_policy,
            "tool_selection": ToolSelectionPolicy.from_categories(
                profile.tool_category_names()
            ).to_dict(),
        }
        return ToolResult(
            f"Sub-agent '{profile.name}' scheduled.",
            control_action=ToolControlAction.schedule_subtask(payload),
        )


class AgentCompleteTool(BaseTool):
    @property
    def name(self) -> str:
        return "agent__complete"

    @property
    def display_name(self) -> str:
        return "Complete task"

    @property
    def description(self) -> str:
        return "Complete an explicit Agent run with its final result; call this only when the requested work is finished."

    @property
    def category(self) -> str:
        return "state"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "result": {"type": "string", "description": "Final result returned to the user or parent Agent."},
            },
            "required": ["result"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        result = str(arguments.get("result") or "").strip()
        if not result:
            return ToolResult("result is required.", is_error=True)
        policy = getattr(getattr(context, "runtime", None), "run_policy", None)
        schema = getattr(policy, "completion_schema", None)
        if schema:
            from core.capabilities.validation import parse_and_validate_output

            _, error = parse_and_validate_output(result, schema)
            if error:
                return ToolResult(f"Completion output failed schema validation: {error}", is_error=True)
        return ToolResult(
            "Completion acknowledged.",
            control_action=ToolControlAction.complete(result),
        )
