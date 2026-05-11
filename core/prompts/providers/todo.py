from __future__ import annotations

from models.state import TaskPriority, TaskStatus

from core.prompts.providers.base import ProviderContext, synthetic_context_message


class TodoProvider:
    name = "todo"
    priority = 25

    def build(self, context: ProviderContext):
        try:
            state = context.conversation.get_state()
            tasks = state.get_active_todos()
            recent = state.recent_completed_todos[-3:]
        except Exception:
            tasks = []
            recent = []
        if not tasks and not recent:
            return []

        lines = ["<todo_state>"]
        for task in tasks:
            status = task.status.value if isinstance(task.status, TaskStatus) else str(task.status or "pending")
            priority = task.priority.value if isinstance(task.priority, TaskPriority) else str(task.priority or "medium")
            tags = " ".join(f"#{tag}" for tag in (task.tags or []) if str(tag).strip())
            suffix = f" {tags}" if tags else ""
            lines.append(f"active: [{status}] ({priority}) {task.content}{suffix} [id:{task.id}]")
        if not tasks and recent:
            lines.append("active: none")
        if recent:
            lines.append("recently_completed_or_cancelled:")
            for item in recent:
                status = item.status.value if isinstance(item.status, TaskStatus) else str(item.status or "completed")
                lines.append(f"- [{status}] {item.content}")
        lines.append("rules: Todo is current progress only, not a plan/report/memory. Keep at most one in_progress item; do not recreate equivalent completed todos.")
        lines.append("</todo_state>")
        return [synthetic_context_message("\n".join(lines), kind=self.name)]
