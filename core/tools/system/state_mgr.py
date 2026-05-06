"""
StateMgrTool: Unified coordinator for cognitive state and context.
Replaces StateManagerTool with added context condensation capabilities.
Delegates specialized logic to TaskService, MemoryService, and SummaryService.
"""

from pathlib import Path
from typing import Dict, Any, List
from core.tools.base import BaseTool, ToolContext, ToolResult
from models.state import SessionState

from core.state.services.task_service import TaskService
from core.state.services.memory_service import MemoryService
from core.state.services.summary_service import SummaryService

class StateMgrTool(BaseTool):
    """
    Unified tool for managing conversation state (summary, tasks, memory) and context window.
    Acts as a facade delegating to specialized services.
    """
    
    @property
    def name(self) -> str:
        return "manage_state"

    @property
    def description(self) -> str:
        return """Manage the conversation's cognitive state and context window. Use this to:
1. Update the 'summary' when context changes significantly
2. Manage 'tasks' (create new ones, update status, mark completed)
3. Store key facts in 'memory' (user preferences, important paths, decisions)
4. Archive context ('archive_context=True') to compress recent history into the summary

Call this tool whenever:
- A task is completed or new tasks are identified
- Important information is gathered
- You want to clear the context window (archive) after a major milestone
"""

    @property
    def group(self) -> str:
        return "modes"

    @property
    def category(self) -> str:
        return "read"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Update the global conversation summary. Provide a complete replacement, not a delta."
                },
                "tasks": {
                    "type": "array",
                    "description": "List of task operations to perform",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["create", "update", "delete"],
                                "description": "Operation type"
                            },
                            "id": {
                                "type": "string",
                                "description": "Task ID (required for update/delete, ignored for create)"
                            },
                            "content": {
                                "type": "string",
                                "description": "Task description"
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed", "cancelled"],
                                "description": "Task status"
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["low", "medium", "high", "urgent"],
                                "description": "Task priority"
                            },
                            "tags": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Categorization tags"
                            }
                        },
                        "required": ["action"]
                    }
                },
                "memory": {
                    "type": "object",
                    "description": "Key-value pairs to remember. Set value to null to forget a key. By default writes to session memory; use memory_tier to target workspace or global memory.",
                    "additionalProperties": {
                        "type": ["string", "null"]
                    }
                },
                "memory_tier": {
                    "type": "string",
                    "enum": ["session", "workspace", "global"],
                    "description": "Which memory tier to write to when 'memory' is provided. 'session' updates SessionState; 'workspace' writes to <work_dir>/.pycat/memory/; 'global' writes to ~/.PyCat/memory/. Default: session."
                },
                "archive_context": {
                    "type": "boolean",
                    "description": "If true, triggers condensation logic to compress recent messages into the summary."
                }
            }
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        """Execute state updates and return feedback"""
        
        # Get current state from context
        state_dict = context.state if context.state else {}
        state = SessionState.from_dict(state_dict)
        
        feedback = []
        current_seq = state_dict.get('_current_seq', 0)  # Injected by runner
        
        # 1. Update Summary (Manual)
        if "summary" in arguments and arguments["summary"]:
            res = SummaryService.update_summary(state, arguments["summary"], current_seq)
            feedback.append(res)

        # 2. Process Task Operations
        if "tasks" in arguments and arguments["tasks"]:
            res = TaskService.handle_ops(state, arguments["tasks"], current_seq)
            feedback.extend(res)

        # 3. Update Memory
        if "memory" in arguments and arguments["memory"]:
            tier = str(arguments.get("memory_tier") or "session").strip().lower()
            if tier in ("workspace", "global"):
                res = self._write_tier_memory(context, arguments["memory"], tier)
                feedback.extend(res)
            else:
                res = MemoryService.handle_updates(state, arguments["memory"], current_seq)
                feedback.extend(res)

        # 4. Condense/Archive Context
        if arguments.get("archive_context"):
            res = await SummaryService.archive_context(
                state, context.llm_client, context.conversation, context.provider, current_seq
            )
            feedback.extend(res)

        # 5. Update the context state dict
        state.last_updated_seq = current_seq
        updated_state_dict = state.to_dict()
        context.state.clear()
        context.state.update(updated_state_dict)

        # 6. Return feedback
        if not feedback:
            return ToolResult("No changes made. Provide summary, tasks, memory or archive_context.")
        
        result_text = "State updated:\n" + "\n".join(feedback)

        return ToolResult(result_text)

    @staticmethod
    def _write_tier_memory(context: ToolContext, updates: Dict[str, Any], tier: str) -> List[str]:
        """Write memory key-value pairs as markdown files to workspace or global memory directory."""
        feedback: list[str] = []
        if tier == "workspace":
            base_dir = Path(context.work_dir or ".") / ".pycat" / "memory"
        else:
            base_dir = Path.home() / ".PyCat" / "memory"

        try:
            base_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            feedback.append(f"Failed to create {tier} memory dir: {e}")
            return feedback

        for key, value in updates.items():
            file_path = base_dir / f"memory__{key}.md"
            if value is None:
                try:
                    if file_path.exists():
                        file_path.unlink()
                        feedback.append(f"Deleted {tier} memory: {key}")
                    else:
                        feedback.append(f"No {tier} memory to delete: {key}")
                except Exception as e:
                    feedback.append(f"Failed to delete {tier} memory {key}: {e}")
            else:
                try:
                    content = f"# {key}\n\n{value}\n"
                    file_path.write_text(content, encoding="utf-8")
                    feedback.append(f"Wrote {tier} memory: {key}")
                except Exception as e:
                    feedback.append(f"Failed to write {tier} memory {key}: {e}")
        return feedback
