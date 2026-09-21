from __future__ import annotations

from typing import Any, Dict

from pycat.core.state.todo import TodoService
from pycat.core.tools.base import BaseTool, ToolContext, ToolResult
from pycat.models.contracts.session_state import SessionState


class ManageTodoTool(BaseTool):
    @property
    def name(self) -> str:
        return "state__todo"

    @property
    def display_name(self) -> str:
        return "更新任务进度"

    @property
    def description(self) -> str:
        return "Maintain a compact user-visible milestone list for the current task."

    @property
    def category(self) -> str:
        return "state"

    @property
    def input_schema(self) -> Dict[str, Any]:
        item_schema = {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Stable item id; required for update."},
                "title": {"type": "string", "description": "Short outcome-oriented milestone."},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed", "cancelled", "blocked"],
                },
                "note": {"type": "string", "description": "Optional concise context or blocking reason."},
                "refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional file, URL, archive, artifact, or tool-result references.",
                },
            },
            "additionalProperties": False,
        }
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["set", "update", "clear", "list"]},
                "items": {"type": "array", "items": item_schema},
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        action = str(arguments.get("action") or "").strip().lower()
        items = arguments.get("items") if isinstance(arguments.get("items"), list) else []
        state = SessionState.from_dict(dict(context.state or {}))
        current_seq = int(context.state.get("_current_seq", 0) or 0)

        if action == "list":
            return ToolResult(self._render(state))
        if action == "clear":
            state.todos = []
            self._sync(context, state, current_seq)
            return ToolResult("Todo list cleared.")
        if action == "set":
            staged = SessionState.from_dict(state.to_dict())
            staged.todos = []
            if any(not isinstance(raw, dict) for raw in items):
                return ToolResult("Every set item must be an object.", is_error=True)
            feedback = TodoService.handle_ops(staged, [{"action": "create", **raw} for raw in items], current_seq)
            if any(line.startswith("Rejected") for line in feedback):
                return ToolResult("\n".join(feedback), is_error=True)
            self._sync(context, staged, current_seq)
            return ToolResult(self._render(staged))
        if action == "update":
            operations = []
            for raw in items:
                if not isinstance(raw, dict) or not str(raw.get("id") or "").strip():
                    return ToolResult("Every update item requires id.", is_error=True)
                operations.append({"action": "update", **raw})
            feedback = TodoService.handle_ops(state, operations, current_seq)
            if any(line.startswith("Rejected") for line in feedback):
                return ToolResult("\n".join(feedback), is_error=True)
            self._sync(context, state, current_seq)
            return ToolResult("\n".join(feedback + [self._render(state)]))
        return ToolResult(f"Unknown todo action: {action}", is_error=True)

    @staticmethod
    def _render(state: SessionState) -> str:
        if not state.todos:
            return "No active todos."
        lines = ["Active todos:"]
        for item in state.todos:
            suffix = f" - {item.note}" if item.note else ""
            refs = f" refs={','.join(item.refs)}" if item.refs else ""
            lines.append(f"- [{item.status.value}] {item.title}{suffix} (id={item.id}){refs}")
        return "\n".join(lines)

    @staticmethod
    def _sync(context: ToolContext, state: SessionState, current_seq: int) -> None:
        state.last_updated_seq = current_seq
        context.state.clear()
        context.state.update(state.to_dict())
        context.state["_current_seq"] = current_seq
