from __future__ import annotations

from pycat.core.context.providers.base import MessageProviderMixin, ProviderContext, context_item
from pycat.core.state.operations import get_active_todos

class TodoProvider(MessageProviderMixin):
    name = "todo"
    priority = 25

    def build_items(self, context: ProviderContext):
        try:
            state = context.conversation.get_state()
            todos = get_active_todos(state)
            recent = state.recent_completed_todos[-3:]
        except Exception:
            todos, recent = [], []
        if not todos and not recent:
            return []
        lines = ["<todo_state>"]
        if todos:
            lines.extend(
                f"- [{todo.status.value}] {todo.title}"
                + (f" - {todo.note}" if todo.note else "")
                + (f" refs={','.join(todo.refs)}" if todo.refs else "")
                + f" [id:{todo.id}]"
                for todo in todos
            )
        if recent:
            lines.append("recently_completed:")
            lines.extend(
                f"- [{item.status.value}] {item.title}"
                + (f" refs={','.join(item.refs)}" if item.refs else "")
                for item in recent
            )
        lines.append("</todo_state>")
        return [context_item("\n".join(lines), kind=self.name, priority=self.priority, item_id="todo:active", required=True)]
