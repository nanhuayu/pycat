from typing import List, Dict, Any
from models.state import SessionState, Task, TaskStatus, TaskPriority

class TaskService:
    @staticmethod
    def prune_terminal_tasks(state: SessionState) -> int:
        original_len = len(state.tasks)
        state.tasks = [
            task for task in state.tasks
            if task.status not in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}
        ]
        return max(0, original_len - len(state.tasks))

    @staticmethod
    def handle_ops(state: SessionState, ops: List[Dict[str, Any]], current_seq: int) -> List[str]:
        feedback = []
        for op in ops:
            action = op.get("action")
            
            if action == "create":
                content = op.get("content", "").strip()
                if not content:
                    feedback.append("⚠️ Skipped create: empty content")
                    continue
                    
                new_task = Task(
                    content=content,
                    status=TaskStatus(op.get("status", "pending")),
                    priority=TaskPriority(op.get("priority", "medium")),
                    tags=op.get("tags", []),
                    created_seq=current_seq,
                    updated_seq=current_seq
                )
                state.tasks.append(new_task)
                feedback.append(f"✅ Created task [{new_task.id}]: {content[:50]}")
                
            elif action == "update":
                task_id = op.get("id")
                if not task_id:
                    feedback.append("⚠️ Skipped update: missing task ID")
                    continue
                    
                task = state.find_task(task_id)
                if not task:
                    feedback.append(f"⚠️ Task [{task_id}] not found")
                    continue
                
                # Apply updates
                update_fields = {}
                if "content" in op: update_fields["content"] = op["content"]
                if "status" in op: update_fields["status"] = op["status"]
                if "priority" in op: update_fields["priority"] = op["priority"]
                if "tags" in op: update_fields["tags"] = op["tags"]
                
                task.update(current_seq, **update_fields)
                feedback.append(f"✅ Updated task [{task_id}]: {list(update_fields.keys())}")
                
            elif action == "delete":
                task_id = op.get("id")
                if not task_id:
                    feedback.append("⚠️ Skipped delete: missing task ID")
                    continue
                    
                original_len = len(state.tasks)
                state.tasks = [t for t in state.tasks if t.id != task_id]
                
                if len(state.tasks) < original_len:
                    feedback.append(f"✅ Deleted task [{task_id}]")
                else:
                    feedback.append(f"⚠️ Task [{task_id}] not found to delete")

        pruned = TaskService.prune_terminal_tasks(state)
        if pruned:
            feedback.append(f"🧹 Removed {pruned} completed/cancelled task(s)")
                    
        return feedback

    @staticmethod
    def ensure_active_task(state: SessionState, current_seq: int) -> bool:
        """Promote the first pending task when the agent has not marked any task active."""
        active = [
            task for task in state.tasks
            if task.status in {TaskStatus.PENDING, TaskStatus.IN_PROGRESS}
        ]
        if not active:
            return False
        if any(task.status == TaskStatus.IN_PROGRESS for task in active):
            return False
        active[0].update(current_seq, status=TaskStatus.IN_PROGRESS)
        state.last_updated_seq = current_seq
        return True

    @staticmethod
    def complete_active_tasks(state: SessionState, current_seq: int, reason: str = "") -> int:
        """Complete all currently active todos before a successful attempt_completion."""
        count = 0
        for task in list(state.tasks):
            if task.status in {TaskStatus.PENDING, TaskStatus.IN_PROGRESS}:
                task.update(current_seq, status=TaskStatus.COMPLETED)
                count += 1
        if reason:
            state.memory["last_completion"] = str(reason or "")[:500]
        if count:
            state.last_updated_seq = current_seq
        TaskService.prune_terminal_tasks(state)
        return count
