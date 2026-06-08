from __future__ import annotations

from models.state import TodoPriority, TodoStatus

from core.prompts.providers.base import MessageProviderMixin, ProviderContext, context_item


class TodoProvider(MessageProviderMixin):
    name = "todo"
    priority = 25

    def build_items(self, context: ProviderContext):
        try:
            state = context.conversation.get_state()
            todos = state.get_active_todos()
            recent = state.recent_completed_todos[-3:]
        except Exception:
            todos = []
            recent = []
        if not todos and not recent:
            return []

        lines = ["<todo_state>"]
        for todo in todos:
            status = todo.status.value if isinstance(todo.status, TodoStatus) else str(todo.status or "pending")
            priority = todo.priority.value if isinstance(todo.priority, TodoPriority) else str(todo.priority or "medium")
            tags = " ".join(f"#{tag}" for tag in (todo.tags or []) if str(tag).strip())
            suffix = f" {tags}" if tags else ""
            detail = f" - {todo.description}" if getattr(todo, "description", "") else ""
            acceptance = f" acceptance={todo.acceptance}" if getattr(todo, "acceptance", "") else ""
            blocked = f" blocked_reason={todo.blocked_reason}" if getattr(todo, "blocked_reason", "") else ""
            lines.append(f"active: [{status}] ({priority}) {todo.title}{detail}{acceptance}{blocked}{suffix} [id:{todo.id}]")
        if not todos and recent:
            lines.append("active: none")
        if recent:
            lines.append("recently_completed_or_cancelled:")
            for item in recent:
                status = item.status.value if isinstance(item.status, TodoStatus) else str(item.status or "completed")
                lines.append(f"- [{status}] {item.title}")
        lines.append("rules: Todo is current progress only, not a plan/report/memory/tool log. Keep at most one in_progress item; use blocked with blocked_reason when needed; do not recreate equivalent completed todos.")
        lines.append("</todo_state>")
        return [context_item("\n".join(lines), kind=self.name, priority=self.priority, item_id="todo:active", required=True)]
