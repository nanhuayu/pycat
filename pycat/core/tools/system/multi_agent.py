"""Delegated-agent tool backed by workspace Mode profiles."""
from __future__ import annotations

import json
from typing import Any, Dict

from pycat.core.capabilities.validation import parse_and_validate_output
from pycat.core.modes.manager import ModeManager
from pycat.core.tools.base import BaseTool, ToolContext, ToolControlAction, ToolResult
from pycat.models.contracts.tooling import ToolSelectionPolicy


class AgentTaskTool(BaseTool):
    """Narrow injected application port; no runtime or repository ownership."""
    def __init__(self, operation=None):
        self.operation = operation

    @property
    def name(self):
        return 'agent__task'

    @property
    def display_name(self):
        return '独立任务'

    @property
    def description(self):
        return ('Submit an explicitly independent task to a new conversation; immediately returns a task reference. '
                'Use agent__run for a short child whose answer is needed in this run. Only one delegation level is allowed. '
                'Provide a self-contained brief and delivery requirements; history is not copied. '
                'Results do not automatically resume this conversation. Query status only when needed; do not busy-poll.')

    @property
    def category(self):
        return 'delegate'

    @property
    def risk(self):
        return 'medium'

    @property
    def input_schema(self):
        return {'type': 'object', 'properties': {
            'action': {'type': 'string', 'enum': ['submit', 'status', 'cancel']},
            'brief': {'type': 'string', 'description': 'Self-contained goal, constraints, relevant context and acceptance evidence.'},
            'read_only': {'type': 'boolean', 'description': 'Allow read/web tools and task-local todo/artifacts; deny shared memory/wiki writes and other actions.'},
            'task_id': {'type': 'string', 'description': 'Returned target conversation ID; omit for status of all own tasks.'}},
            'required': ['action'], 'additionalProperties': False}

    async def execute(self, arguments, context):
        if self.operation is None:
            return ToolResult('Independent tasks require an application host.', is_error=True)
        result = await self.operation(arguments, context)
        if isinstance(result, list):
            result = {'tasks': result[:20], 'total': len(result), 'truncated': len(result) > 20}
        if isinstance(result, dict) and len(result.get('result', '')) > 4000:
            result = {**result, 'result': result['result'][:4000], 'result_truncated': True}
        return ToolResult(json.dumps(result, ensure_ascii=False))


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
        return self.input_schema_for_work_dir("", include_profiles=False)

    @staticmethod
    def profile_names(work_dir: str, *, data_dir: str | None = None) -> list[str]:
        try:
            return [profile.slug for profile in ModeManager(work_dir, data_dir=data_dir).list_subagent_profiles()]
        except Exception:
            return []

    @classmethod
    def input_schema_for_work_dir(cls, work_dir: str, *, data_dir: str | None = None, include_profiles: bool = True) -> Dict[str, Any]:
        profile_schema: dict[str, Any] = {
            "type": "string",
            "description": "Configured sub-agent profile.",
        }
        profiles = cls.profile_names(work_dir, data_dir=data_dir) if include_profiles else []
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
        profiles = {item.slug: item for item in ModeManager(context.work_dir, data_dir=context.data_dir).list_subagent_profiles()}
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

            _, error = parse_and_validate_output(result, schema)
            if error:
                return ToolResult(f"Completion output failed schema validation: {error}", is_error=True)
        return ToolResult(
            "Completion acknowledged.",
            control_action=ToolControlAction.complete(result),
        )
