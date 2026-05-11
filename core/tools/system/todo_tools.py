"""Explicit todo management tool backed by SessionState.tasks."""

from typing import Any, Dict, List

from core.state.services.task_service import TaskService
from core.tools.base import BaseTool, ToolContext, ToolResult
from models.state import SessionState, TaskStatus


class ManageTodoTool(BaseTool):
    """Maintain the current session todo list as lightweight progress state."""

    @property
    def name(self) -> str:
        return "manage_todo"

    @property
    def description(self) -> str:
        return (
            "Maintain the explicit current-task todo list. Todos are lightweight progress state, not plans, reports, durable memory, or proof of completion. "
            "Use todos for user-visible milestones during complex work, keep at most one item in_progress, and do not recreate equivalent todos after they were completed.\n\n"
            "When to use:\n"
            "- Use action=set with items=[...] to establish or synchronize visible progress milestones when a task needs ongoing tracking.\n"
            "- Use action=update to mark progress, complete a milestone, or start the next milestone.\n"
            "- Use action=clear when the task is finished and the final response/artifact is ready. Completed/cancelled todos are compacted into a short recent history.\n"
            "- Good items are outcome milestones visible to the user, e.g. analyze evidence, implement patch, verify behavior, produce report.\n\n"
            "When not to use:\n"
            "- Do not use for one-step answers, casual chat, or durable preferences/facts. Use manage_memory for stable reusable facts and manage_artifact for long-form outputs.\n"
            "- Do not add purely operational support actions such as searching, grepping, formatting, or reading files as todo items; todos should be user-meaningful milestones.\n"
            "- If a final report/plan artifact already satisfies the request, finish or clear todos instead of rebuilding the same list.\n\n"
            "Actions:\n"
            "- set: Idempotently synchronize active todos from items[]\n"
            "- update: Update one or more existing todos by id or content\n"
            "- clear: Remove all active todos\n"
            "- list: Show active and recent todos"
        )

    @property
    def category(self) -> str:
        return "manage"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["set", "update", "clear", "list"],
                    "description": "The todo action to perform.",
                },
                "id": {
                    "type": "string",
                    "description": "Todo id. Required for update/delete.",
                },
                "content": {
                    "type": "string",
                    "description": "Todo content. Required for create; optional for update.",
                },
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed", "cancelled"],
                    "description": "Todo status.",
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "urgent"],
                    "description": "Todo priority.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional todo tags.",
                },
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed", "cancelled"],
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["low", "medium", "high", "urgent"],
                            },
                            "tags": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["content"],
                    },
                    "description": "Items for action=set or action=update. set synchronizes active todos; update changes matching todos by id or content.",
                },
                "reason": {
                    "type": "string",
                    "description": "Brief reason for changing todo state.",
                },
            },
            "required": ["action"],
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        action = str(arguments.get("action", "") or "").strip().lower()
        state = SessionState.from_dict(dict(context.state or {}))
        seq = int((context.state or {}).get("_current_seq", 0))

        if action == "list":
            if not state.tasks and not state.recent_completed_todos:
                return ToolResult("No todos in this session.")
            return ToolResult(self._render_todos(state, include_recent=True))

        if action == "clear":
            count = len(state.tasks)
            state.tasks.clear()
            state.last_updated_seq = seq
            self._sync_context_state(context.state, state)
            return ToolResult(f"Cleared {count} todo(s).")

        if action == "set":
            items = arguments.get("items") or []
            if not isinstance(items, list):
                return ToolResult("items must be an array for action=set.", is_error=True)
            feedback = self._set_todos(state, items, seq)
            state.last_updated_seq = seq
            self._sync_context_state(context.state, state)
            return ToolResult(self._render_feedback(feedback, state))

        if action == "update":
            items = arguments.get("items")
            if isinstance(items, list):
                ops = self._ops_from_update_items(items)
            else:
                op: Dict[str, Any] = {"action": "update"}
                for key in ("id", "content", "status", "priority", "tags"):
                    if key in arguments:
                        op[key] = arguments.get(key)
                ops = [op]
            feedback = TaskService.handle_ops(state, self._normalize_in_progress(ops), seq)
            self._enforce_single_in_progress(state)
            state.last_updated_seq = seq
            self._sync_context_state(context.state, state)
            return ToolResult(self._render_feedback(feedback, state))

        return ToolResult(f"Unknown action: {action}", is_error=True)

    @staticmethod
    def _sync_context_state(context_state: Dict[str, object], state: SessionState) -> None:
        context_state.clear()
        context_state.update(state.to_dict())

    @staticmethod
    def _ops_from_items(items: List[Any]) -> List[Dict[str, Any]]:
        ops: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            op: Dict[str, Any] = {
                "action": "create",
                "content": str(item.get("content") or "").strip(),
                "status": str(item.get("status") or "pending"),
                "priority": str(item.get("priority") or "medium"),
            }
            if "tags" in item:
                op["tags"] = item.get("tags") or []
            ops.append(op)
        return ops

    @staticmethod
    def _ops_from_update_items(items: List[Any]) -> List[Dict[str, Any]]:
        ops: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            op: Dict[str, Any] = {"action": "update"}
            for key in ("id", "content", "status", "priority", "tags"):
                if key in item:
                    op[key] = item.get(key)
            ops.append(op)
        return ops

    @staticmethod
    def _content_key(value: Any) -> str:
        return " ".join(str(value or "").strip().casefold().split())

    def _set_todos(self, state: SessionState, items: List[Any], seq: int) -> List[str]:
        incoming = [item for item in items if isinstance(item, dict) and str(item.get("content") or "").strip()]
        incoming_ops = self._normalize_in_progress(self._ops_from_items(incoming))
        incoming_keys = {self._content_key(op.get("content")) for op in incoming_ops}

        feedback: List[str] = []
        delete_ops = [
            {"action": "delete", "id": task.id}
            for task in state.tasks
            if self._content_key(task.content) not in incoming_keys
        ]
        if delete_ops:
            feedback.extend(TaskService.handle_ops(state, delete_ops, seq))

        ops: List[Dict[str, Any]] = []
        for op in incoming_ops:
            existing = next((task for task in state.tasks if self._content_key(task.content) == self._content_key(op.get("content"))), None)
            if existing:
                update_op: Dict[str, Any] = {"action": "update", "id": existing.id}
                for key in ("content", "status", "priority", "tags"):
                    if key in op:
                        update_op[key] = op[key]
                ops.append(update_op)
            else:
                ops.append(op)
        feedback.extend(TaskService.handle_ops(state, ops, seq))
        self._enforce_single_in_progress(state)
        return feedback or ["Todo state unchanged."]

    @staticmethod
    def _sanitize_ops(ops: List[Any]) -> List[Dict[str, Any]]:
        sanitized: List[Dict[str, Any]] = []
        for item in ops:
            if not isinstance(item, dict):
                continue
            op: Dict[str, Any] = {"action": str(item.get("action") or "").strip().lower()}
            for key in ("id", "content", "status", "priority", "tags"):
                if key in item:
                    op[key] = item.get(key)
            sanitized.append(op)
        return sanitized

    @staticmethod
    def _normalize_in_progress(ops: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen_in_progress = False
        normalized: List[Dict[str, Any]] = []
        for op in ops:
            item = dict(op)
            if str(item.get("status") or "") == TaskStatus.IN_PROGRESS.value:
                if seen_in_progress:
                    item["status"] = TaskStatus.PENDING.value
                seen_in_progress = True
            normalized.append(item)
        return normalized

    @staticmethod
    def _enforce_single_in_progress(state: SessionState) -> None:
        seen = False
        for task in state.tasks:
            if task.status != TaskStatus.IN_PROGRESS:
                continue
            if not seen:
                seen = True
                continue
            task.status = TaskStatus.PENDING

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
        for task in state.tasks:
            status = task.status.value if isinstance(task.status, TaskStatus) else str(task.status)
            tags = f" tags={', '.join(task.tags)}" if task.tags else ""
            lines.append(f"- [{task.id}] {status}/{task.priority.value}: {task.content}{tags}")
        if include_recent and state.recent_completed_todos:
            if lines:
                lines.append("Recent completed/cancelled todos:")
            for item in state.recent_completed_todos[-3:]:
                status = item.status.value if isinstance(item.status, TaskStatus) else str(item.status)
                lines.append(f"- [{status}] {item.content}")
        return "\n".join(lines)
