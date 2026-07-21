from __future__ import annotations

from typing import Any, Dict, List

from core.state.operations import find_todo, remember_completed_todo
from models.contracts.session_state import SessionState, TodoItem, TodoStatus


class TodoService:
    TERMINAL = {TodoStatus.COMPLETED, TodoStatus.CANCELLED}

    @staticmethod
    def prune_terminal_todos(state: SessionState, current_seq: int = 0) -> int:
        terminal = [todo for todo in state.todos if todo.status in TodoService.TERMINAL]
        for todo in terminal:
            remember_completed_todo(state, todo, current_seq)
        state.todos = [todo for todo in state.todos if todo.status not in TodoService.TERMINAL]
        return len(terminal)

    @staticmethod
    def _normalize_title(title: str) -> str:
        return " ".join(str(title or "").strip().casefold().split())

    @staticmethod
    def _find_active_by_title(state: SessionState, title: str) -> TodoItem | None:
        normalized = TodoService._normalize_title(title)
        return next(
            (todo for todo in state.todos if TodoService._normalize_title(todo.title) == normalized),
            None,
        ) if normalized else None

    @staticmethod
    def handle_ops(state: SessionState, ops: List[Dict[str, Any]], current_seq: int) -> List[str]:
        feedback: list[str] = []
        for op in ops:
            action = str(op.get("action") or "").strip().lower()
            if action == "create":
                title = str(op.get("title") or "").strip()
                if not title:
                    feedback.append("Skipped create: title is required")
                    continue
                existing = TodoService._find_active_by_title(state, title)
                values = TodoService._fields(op)
                if existing:
                    existing.update(current_seq, **values)
                    feedback.append(f"Reused todo [{existing.id}]")
                    continue
                todo = TodoItem(
                    id=str(op.get("id") or "").strip() or TodoItem(title=title).id,
                    title=title,
                    status=TodoService._status(op.get("status")),
                    note=str(op.get("note") or "").strip(),
                    refs=TodoService._refs(op.get("refs")),
                    created_seq=current_seq,
                    updated_seq=current_seq,
                )
                state.todos.append(todo)
                feedback.append(f"Created todo [{todo.id}]")
            elif action == "update":
                todo_id = str(op.get("id") or "").strip()
                todo = find_todo(state, todo_id) if todo_id else None
                if todo is None:
                    feedback.append(f"Todo not found: {todo_id}")
                    continue
                todo.update(current_seq, **TodoService._fields(op))
                feedback.append(f"Updated todo [{todo.id}]")
            elif action == "delete":
                todo_id = str(op.get("id") or "").strip()
                before = len(state.todos)
                state.todos = [todo for todo in state.todos if todo.id != todo_id]
                feedback.append(f"Deleted todo [{todo_id}]" if len(state.todos) < before else f"Todo not found: {todo_id}")

        TodoService.enforce_single_in_progress(state)
        TodoService.prune_terminal_todos(state, current_seq)
        return feedback

    @staticmethod
    def enforce_single_in_progress(state: SessionState) -> None:
        active_seen = False
        for todo in state.todos:
            if todo.status != TodoStatus.IN_PROGRESS:
                continue
            if active_seen:
                todo.status = TodoStatus.PENDING
            active_seen = True

    @staticmethod
    def _fields(op: Dict[str, Any]) -> Dict[str, Any]:
        values: Dict[str, Any] = {}
        for key in ("title", "note"):
            if key in op:
                values[key] = str(op.get(key) or "").strip()
        if "refs" in op:
            values["refs"] = TodoService._refs(op.get("refs"))
        if "status" in op:
            values["status"] = TodoService._status(op.get("status"))
        return values

    @staticmethod
    def _status(value: Any) -> TodoStatus:
        raw = str(getattr(value, "value", value) or TodoStatus.PENDING.value).strip().lower()
        return TodoStatus(raw) if raw in {item.value for item in TodoStatus} else TodoStatus.PENDING

    @staticmethod
    def _refs(value: Any) -> list[str]:
        result: list[str] = []
        for item in value or []:
            text = str(item or "").strip()
            if text and text not in result:
                result.append(text)
            if len(result) >= 12:
                break
        return result
