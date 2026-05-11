"""
StateMgrTool: coordinator for conversation summary and context archival.
"""

from typing import Dict, Any
from core.tools.base import BaseTool, ToolContext, ToolResult
from models.state import SessionState

from core.state.services.summary_service import SummaryService

class StateMgrTool(BaseTool):
    """
    Tool for managing conversation summary and context window archival.
    Todo state is managed by manage_todo. Session artifacts are managed by manage_artifact.
    Memory is managed by manage_memory.
    """
    
    @property
    def name(self) -> str:
        return "manage_state"

    @property
    def description(self) -> str:
        return """Manage the conversation summary and context window. Use this to:
1. Replace the rolling 'summary' when context changes significantly
2. Archive context ('archive_context=True') to compress recent history into the summary

Call this tool whenever:
- You reach a major milestone and need to preserve concise conversation context
- You want to clear the context window by archiving recent messages into summary

Use manage_todo for current task status, manage_artifact for plans/reports/notes, and manage_memory for durable reusable facts.
"""

    @property
    def category(self) -> str:
        return "manage"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Update the global conversation summary. Provide a complete replacement, not a delta."
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

        # 2. Condense/Archive Context
        if arguments.get("archive_context"):
            res = await SummaryService.archive_context(
                state, context.llm_client, context.conversation, context.provider, current_seq
            )
            feedback.extend(res)

        # 3. Update the context state dict
        state.last_updated_seq = current_seq
        updated_state_dict = state.to_dict()
        context.state.clear()
        context.state.update(updated_state_dict)

        # 4. Return feedback
        if not feedback:
            return ToolResult("No changes made. Provide summary or archive_context.")
        
        result_text = "State updated:\n" + "\n".join(feedback)

        return ToolResult(result_text)
