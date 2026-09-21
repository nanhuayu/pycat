from __future__ import annotations

from typing import Any, Dict, List

from pycat.core.state.operations import find_todo, remember_completed_todo
from pycat.models.contracts.session_state import SessionState, TodoItem, TodoStatus


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
        staged = SessionState.from_dict(state.to_dict())
        try:
            feedback = TodoService._stage_ops(staged, ops, current_seq)
            ids = [todo.id for todo in staged.todos]
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate todo ids")
            if sum(todo.status == TodoStatus.IN_PROGRESS for todo in staged.todos) > 1:
                raise ValueError("at most one milestone may be in_progress")
            TodoService.prune_terminal_todos(staged, current_seq)
        except (TypeError, ValueError) as exc:
            return [f"Rejected todo batch: {exc}"]
        state.todos = staged.todos
        state.recent_completed_todos = staged.recent_completed_todos
        return feedback

    @staticmethod
    def _stage_ops(state: SessionState, ops: List[Dict[str, Any]], current_seq: int) -> List[str]:
        if not isinstance(ops, list) or any(not isinstance(op, dict) for op in ops):
            raise ValueError("operations must be an array of objects")
        feedback: list[str] = []
        for op in ops:
            action = str(op.get("action") or "").strip().lower()
            if action == "create":
                title = str(op.get("title") or "").strip()
                if not title:
                    raise ValueError("title is required")
                if op.get("id") and find_todo(state, str(op["id"])):
                    raise ValueError("duplicate todo ids")
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
                    raise ValueError(f"Todo not found: {todo_id}")
                if "title" in op and not str(op["title"] or "").strip():
                    raise ValueError("title is required")
                todo.update(current_seq, **TodoService._fields(op))
                feedback.append(f"Updated todo [{todo.id}]")
            elif action == "delete":
                todo_id = str(op.get("id") or "").strip()
                before = len(state.todos)
                state.todos = [todo for todo in state.todos if todo.id != todo_id]
                if len(state.todos) == before:
                    raise ValueError(f"Todo not found: {todo_id}")
                feedback.append(f"Deleted todo [{todo_id}]" if len(state.todos) < before else f"Todo not found: {todo_id}")
            else:
                raise ValueError(f"unknown todo action: {action}")
        return feedback

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
        return TodoStatus(raw)

    @staticmethod
    def _refs(value: Any) -> list[str]:
        if value is not None and (not isinstance(value, list) or len(value) > 12):
            raise ValueError("refs must be an array of at most 12 references")
        result: list[str] = []
        for item in value or []:
            text = str(item or "").strip()
            if text and text not in result:
                result.append(text)
            if len(result) >= 12:
                break
        return result
