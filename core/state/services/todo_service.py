from __future__ import annotations

from typing import Any, Dict, List

from models.state import SessionState, TodoItem, TodoPriority, TodoStatus


class TodoService:
    """Structured todo state operations."""

    TERMINAL = {TodoStatus.COMPLETED, TodoStatus.CANCELLED}

    @staticmethod
    def prune_terminal_todos(state: SessionState, current_seq: int = 0) -> int:
        original_len = len(state.todos)
        for todo in state.todos:
            if todo.status in TodoService.TERMINAL:
                state.remember_completed_todo(todo, current_seq)
        state.todos = [todo for todo in state.todos if todo.status not in TodoService.TERMINAL]
        return max(0, original_len - len(state.todos))

    @staticmethod
    def _normalize_title(title: str) -> str:
        return " ".join(str(title or "").strip().casefold().split())

    @staticmethod
    def _find_active_by_title(state: SessionState, title: str) -> TodoItem | None:
        normalized = TodoService._normalize_title(title)
        if not normalized:
            return None
        for todo in state.todos:
            if TodoService._normalize_title(todo.title) == normalized:
                return todo
        return None

    @staticmethod
    def handle_ops(state: SessionState, ops: List[Dict[str, Any]], current_seq: int) -> List[str]:
        feedback: list[str] = []
        for op in ops:
            action = str(op.get("action") or "").strip().lower()
            if action == "create":
                title = str(op.get("title") or "").strip()
                if not title:
                    feedback.append("Skipped create: empty title")
                    continue

                existing = TodoService._find_active_by_title(state, title)
                fields = TodoService._fields_from_op(op)
                if existing:
                    existing.update(current_seq, **fields)
                    feedback.append(f"Reused todo [{existing.id}]: {title[:60]}")
                    continue

                todo = TodoItem(
                    title=title,
                    description=str(op.get("description") or "").strip(),
                    status=TodoService._status(op.get("status")),
                    priority=TodoService._priority(op.get("priority")),
                    kind=str(op.get("kind") or "").strip(),
                    acceptance=str(op.get("acceptance") or "").strip(),
                    depends_on=TodoService._string_list(op.get("depends_on")),
                    evidence_refs=TodoService._string_list(op.get("evidence_refs")),
                    blocked_reason=str(op.get("blocked_reason") or "").strip(),
                    tags=TodoService._string_list(op.get("tags")),
                    created_seq=current_seq,
                    updated_seq=current_seq,
                )
                state.todos.append(todo)
                feedback.append(f"Created todo [{todo.id}]: {title[:60]}")
                continue

            if action == "update":
                todo_id = str(op.get("id") or "").strip()
                todo = state.find_todo(todo_id) if todo_id else TodoService._find_active_by_title(state, op.get("title") or "")
                if not todo:
                    feedback.append(f"Todo not found: {todo_id or op.get('title') or ''}")
                    continue
                fields = TodoService._fields_from_op(op)
                todo.update(current_seq, **fields)
                feedback.append(f"Updated todo [{todo.id}]: {', '.join(fields.keys()) or 'no fields'}")
                continue

            if action == "delete":
                todo_id = str(op.get("id") or "").strip()
                if not todo_id:
                    feedback.append("Skipped delete: missing todo id")
                    continue
                before = len(state.todos)
                state.todos = [todo for todo in state.todos if todo.id != todo_id]
                feedback.append(f"Deleted todo [{todo_id}]" if len(state.todos) < before else f"Todo not found: {todo_id}")

        TodoService.enforce_single_in_progress(state)
        pruned = TodoService.prune_terminal_todos(state, current_seq)
        if pruned:
            feedback.append(f"Compacted {pruned} terminal todo(s) into recent history")
        return feedback

    @staticmethod
    def enforce_single_in_progress(state: SessionState) -> None:
        seen = False
        for todo in state.todos:
            if todo.status != TodoStatus.IN_PROGRESS:
                continue
            if not seen:
                seen = True
                continue
            todo.status = TodoStatus.PENDING

    @staticmethod
    def _fields_from_op(op: Dict[str, Any]) -> Dict[str, Any]:
        fields: Dict[str, Any] = {}
        if "title" in op:
            fields["title"] = str(op.get("title") or "").strip()
        for key in ("description", "kind", "acceptance", "blocked_reason"):
            if key in op:
                fields[key] = str(op.get(key) or "").strip()
        if "status" in op:
            fields["status"] = TodoService._status(op.get("status"))
        if "priority" in op:
            fields["priority"] = TodoService._priority(op.get("priority"))
        for key in ("depends_on", "evidence_refs", "tags"):
            if key in op:
                fields[key] = TodoService._string_list(op.get(key))
        return fields

    @staticmethod
    def _status(value: Any) -> TodoStatus:
        raw = str(value or TodoStatus.PENDING.value).strip()
        return TodoStatus(raw) if raw in {item.value for item in TodoStatus} else TodoStatus.PENDING

    @staticmethod
    def _priority(value: Any) -> TodoPriority:
        raw = str(value or TodoPriority.MEDIUM.value).strip()
        return TodoPriority(raw) if raw in {item.value for item in TodoPriority} else TodoPriority.MEDIUM

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]
