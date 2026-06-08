"""Explicit todo management tool backed by SessionState.todos."""

from __future__ import annotations

from typing import Any, Dict, List

from core.state.services.todo_service import TodoService
from core.tools.base import BaseTool, ToolContext, ToolResult
from models.state import SessionState, TodoPriority, TodoStatus


TODO_FIELDS = (
    "id",
    "title",
    "description",
    "status",
    "priority",
    "kind",
    "acceptance",
    "depends_on",
    "evidence_refs",
    "blocked_reason",
    "tags",
)


class ManageTodoTool(BaseTool):
    """Maintain the current session todo list as structured progress state."""

    @property
    def name(self) -> str:
        return "state__todo"

    @property
    def description(self) -> str:
        return (
            "Maintain the explicit current-task todo list. Todos are user-visible milestones and acceptance checkpoints, "
            "not plans, reports, durable memory, or a log of tool operations.\n\n"
            "Use todos when work spans multiple meaningful stages. Keep exactly one item in_progress unless blocked. "
            "Use blocked with blocked_reason when progress needs external input or a prerequisite.\n\n"
            "Good todos name deliverables and checks: analyze root cause, implement patch, verify behavior, produce report. "
            "Bad todos are implementation noise: grep files, read docs, run formatter, search web.\n\n"
            "Actions: set synchronizes active todos; update changes existing todos; clear removes active todos; list shows active and recent todos."
        )

    @property
    def category(self) -> str:
        return "manage"

    @property
    def input_schema(self) -> Dict[str, Any]:
        item_schema = {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "title": {"type": "string", "description": "Short milestone title."},
                "description": {"type": "string", "description": "Optional detail explaining the milestone."},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed", "cancelled", "blocked"],
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "urgent"],
                },
                "kind": {
                    "type": "string",
                    "description": "Optional kind such as research, analysis, edit, verify, write.",
                },
                "acceptance": {
                    "type": "string",
                    "description": "Observable completion condition for this milestone.",
                },
                "depends_on": {"type": "array", "items": {"type": "string"}},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
                "blocked_reason": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title"],
            "additionalProperties": False,
        }
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["set", "update", "clear", "list"],
                    "description": "The todo action to perform.",
                },
                "id": {"type": "string", "description": "Todo id for update."},
                "title": {"type": "string", "description": "Todo title for update."},
                "description": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed", "cancelled", "blocked"],
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "urgent"],
                },
                "kind": {"type": "string"},
                "acceptance": {"type": "string"},
                "depends_on": {"type": "array", "items": {"type": "string"}},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
                "blocked_reason": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "items": {
                    "type": "array",
                    "items": item_schema,
                    "description": "Items for action=set or action=update.",
                },
                "reason": {"type": "string", "description": "Brief reason for changing todo state."},
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        action = str(arguments.get("action", "") or "").strip().lower()
        state = SessionState.from_dict(dict(context.state or {}))
        seq = int((context.state or {}).get("_current_seq", 0))

        if action == "list":
            if not state.todos and not state.recent_completed_todos:
                return ToolResult("No todos in this session.")
            return ToolResult(self._render_todos(state, include_recent=True))

        if action == "clear":
            count = len(state.todos)
            state.todos.clear()
            state.last_updated_seq = seq
            self._sync_context_state(context.state, state, seq)
            return ToolResult(f"Cleared {count} todo(s).")

        if action == "set":
            items = arguments.get("items") or []
            if not isinstance(items, list):
                return ToolResult("items must be an array for action=set.", is_error=True)
            feedback = self._set_todos(state, items, seq)
            state.last_updated_seq = seq
            self._sync_context_state(context.state, state, seq)
            return ToolResult(self._render_feedback(feedback, state))

        if action == "update":
            items = arguments.get("items")
            ops = self._ops_from_update_items(items) if isinstance(items, list) else [self._op_from_arguments(arguments)]
            feedback = TodoService.handle_ops(state, self._normalize_in_progress(ops), seq)
            TodoService.enforce_single_in_progress(state)
            state.last_updated_seq = seq
            self._sync_context_state(context.state, state, seq)
            return ToolResult(self._render_feedback(feedback, state))

        return ToolResult(f"Unknown action: {action}", is_error=True)

    @staticmethod
    def _sync_context_state(context_state: Dict[str, object], state: SessionState, seq: int) -> None:
        context_state.clear()
        context_state.update(state.to_dict())
        context_state["_current_seq"] = seq

    @staticmethod
    def _op_from_arguments(arguments: Dict[str, Any]) -> Dict[str, Any]:
        op: Dict[str, Any] = {"action": "update"}
        for key in TODO_FIELDS:
            if key in arguments:
                op[key] = arguments.get(key)
        return op

    @classmethod
    def _ops_from_items(cls, items: List[Any]) -> List[Dict[str, Any]]:
        ops: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            op = {"action": "create"}
            for key in TODO_FIELDS:
                if key in item:
                    op[key] = item.get(key)
            ops.append(op)
        return ops

    @classmethod
    def _ops_from_update_items(cls, items: List[Any]) -> List[Dict[str, Any]]:
        ops: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            op = {"action": "update"}
            for key in TODO_FIELDS:
                if key in item:
                    op[key] = item.get(key)
            ops.append(op)
        return ops

    @staticmethod
    def _title_key(value: Any) -> str:
        return " ".join(str(value or "").strip().casefold().split())

    def _set_todos(self, state: SessionState, items: List[Any], seq: int) -> List[str]:
        incoming = [
            item for item in items
            if isinstance(item, dict) and str(item.get("title") or "").strip()
        ]
        incoming_ops = self._normalize_in_progress(self._ops_from_items(incoming))
        incoming_keys = {self._title_key(op.get("title")) for op in incoming_ops}

        feedback: List[str] = []
        delete_ops = [
            {"action": "delete", "id": todo.id}
            for todo in state.todos
            if self._title_key(todo.title) not in incoming_keys
        ]
        if delete_ops:
            feedback.extend(TodoService.handle_ops(state, delete_ops, seq))

        ops: List[Dict[str, Any]] = []
        for op in incoming_ops:
            title = op.get("title")
            existing = next((todo for todo in state.todos if self._title_key(todo.title) == self._title_key(title)), None)
            if existing:
                update_op = {"action": "update", "id": existing.id}
                update_op.update({key: value for key, value in op.items() if key != "action"})
                ops.append(update_op)
            else:
                ops.append(op)
        feedback.extend(TodoService.handle_ops(state, ops, seq))
        TodoService.enforce_single_in_progress(state)
        return feedback or ["Todo state unchanged."]

    @staticmethod
    def _normalize_in_progress(ops: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen = False
        normalized: List[Dict[str, Any]] = []
        for op in ops:
            item = dict(op)
            if str(item.get("status") or "") == TodoStatus.IN_PROGRESS.value:
                if seen:
                    item["status"] = TodoStatus.PENDING.value
                seen = True
            normalized.append(item)
        return normalized

    def _render_feedback(self, feedback: List[str], state: SessionState) -> str:
        lines = list(feedback or ["No todo changes applied."])
        active = self._render_todos(state)
        if active:
            lines.append("Current todos:")
            lines.append(active)
        return "\n".join(lines)

    @staticmethod
    def _render_todos(state: SessionState, *, include_recent: bool = False) -> str:
        lines = []
        for todo in state.todos:
            status = todo.status.value if isinstance(todo.status, TodoStatus) else str(todo.status)
            priority = todo.priority.value if isinstance(todo.priority, TodoPriority) else str(todo.priority)
            parts = [f"- [{todo.id}] {status}/{priority}: {todo.title}"]
            if todo.description:
                parts.append(f"  description: {todo.description}")
            if todo.acceptance:
                parts.append(f"  acceptance: {todo.acceptance}")
            if todo.depends_on:
                parts.append(f"  depends_on: {', '.join(todo.depends_on)}")
            if todo.evidence_refs:
                parts.append(f"  evidence_refs: {', '.join(todo.evidence_refs[:4])}")
            if todo.blocked_reason:
                parts.append(f"  blocked_reason: {todo.blocked_reason}")
            if todo.tags:
                parts.append(f"  tags: {', '.join(todo.tags)}")
            lines.append("\n".join(parts))
        if include_recent and state.recent_completed_todos:
            if lines:
                lines.append("Recent completed/cancelled todos:")
            for item in state.recent_completed_todos[-3:]:
                status = item.status.value if isinstance(item.status, TodoStatus) else str(item.status)
                lines.append(f"- [{status}] {item.title}")
        return "\n".join(lines)
