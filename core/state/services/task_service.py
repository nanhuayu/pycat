from typing import List, Dict, Any
from models.state import SessionState, Task, TaskStatus, TaskPriority

class TaskService:
    @staticmethod
    def prune_terminal_tasks(state: SessionState, current_seq: int = 0) -> int:
        original_len = len(state.tasks)
        for task in state.tasks:
            if task.status in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}:
                state.remember_completed_todo(task, current_seq)
        state.tasks = [
            task for task in state.tasks
            if task.status not in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}
        ]
        return max(0, original_len - len(state.tasks))

    @staticmethod
    def _normalize_content(content: str) -> str:
        return " ".join(str(content or "").strip().casefold().split())

    @staticmethod
    def _find_active_by_content(state: SessionState, content: str) -> Task | None:
        normalized = TaskService._normalize_content(content)
        if not normalized:
            return None
        for task in state.tasks:
            if TaskService._normalize_content(task.content) == normalized:
                return task
        return None

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

                existing = TaskService._find_active_by_content(state, content)
                if existing:
                    update_fields = {}
                    if "status" in op: update_fields["status"] = op["status"]
                    if "priority" in op: update_fields["priority"] = op["priority"]
                    if "tags" in op: update_fields["tags"] = op["tags"]
                    if update_fields:
                        existing.update(current_seq, **update_fields)
                        feedback.append(f"✅ Reused existing task [{existing.id}]: {content[:50]}")
                    else:
                        feedback.append(f"ℹ️ Task already exists [{existing.id}]: {content[:50]}")
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
                task = state.find_task(task_id) if task_id else TaskService._find_active_by_content(state, op.get("content", ""))
                if not task:
                    feedback.append(f"⚠️ Task [{task_id or op.get('content', '')}] not found")
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

        pruned = TaskService.prune_terminal_tasks(state, current_seq)
        if pruned:
            feedback.append(f"🧹 Compacted {pruned} completed/cancelled todo(s) into recent history")
                    
        return feedback

