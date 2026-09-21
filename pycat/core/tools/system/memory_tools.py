from __future__ import annotations

from typing import Any, Dict

from pycat.core.memory.service import MemoryService
from pycat.core.tools.base import BaseTool, ToolContext, ToolResult

_WRITE_ACTIONS = {"add", "replace", "remove", "apply_batch"}


class ManageMemoryTool(BaseTool):
    """Curate the two durable memory files (project notes + user profile)."""

    @property
    def name(self) -> str:
        return "state__memory"

    @property
    def display_name(self) -> str:
        return "管理记忆"

    @property
    def description(self) -> str:
        return (
            "Read and curate durable two-file memory. target=memory holds project "
            "notes (workspace); target=user holds the user profile (global). Writes "
            "persist immediately and inject on the next run."
        )

    @property
    def category(self) -> str:
        return "state"

    def assess_risk(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        return "medium" if str(arguments.get("action") or "").lower() in _WRITE_ACTIONS else "low"

    def approval_message(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        return f"Update {arguments.get('target') or 'memory'} memory ({arguments.get('action')})?"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["read", "add", "replace", "remove", "apply_batch"],
                },
                "target": {
                    "type": "string",
                    "enum": ["memory", "user"],
                    "description": "memory = project notes (workspace); user = user profile (global).",
                },
                "content": {"type": "string", "description": "Entry content for add."},
                "entry_id": {"type": "string", "description": "Stable id from read; selects an entry for read, replace or remove."},
                "expected_digest": {"type": "string", "description": "Version from read; required for conflict-safe edits."},
                "old_text": {"type": "string", "description": "Exact existing entry text for replace/remove."},
                "new_text": {"type": "string", "description": "Replacement entry text for replace."},
                "operations": {
                    "type": "array",
                    "maxItems": 12,
                    "description": "Batch operations for apply_batch: {op: add|replace|remove, content, old_text, new_text, reason}.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {"type": "string", "enum": ["add", "replace", "remove"]},
                            "content": {"type": "string"},
                            "old_text": {"type": "string"},
                            "new_text": {"type": "string"},
                            "reason": {"type": "string"},
                            "entry_id": {"type": "string"},
                        },
                        "required": ["op"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        action = str(arguments.get("action") or "").strip().lower()
        operations = arguments.get("operations")
        if operations is not None and (not isinstance(operations, list) or any(not isinstance(op, dict) for op in operations)):
            return ToolResult("operations must be an array of valid objects", is_error=True)
        ok, message = MemoryService.handle_tool_action(
            work_dir=context.work_dir,
            action=action,
            target=str(arguments.get("target") or "memory"),
            content=str(arguments.get("content") or ""),
            old_text=str(arguments.get("old_text") or ""),
            new_text=str(arguments.get("new_text") or ""),
            operations=operations,
            conversation=getattr(context, "conversation", None),
            entry_id=str(arguments.get("entry_id") or ""),
            expected_digest=arguments.get("expected_digest"),
            data_dir=context.data_dir,
        )
        return ToolResult(message, is_error=not ok,
                          metadata={"changed_domains": ["memory"]} if ok and action in _WRITE_ACTIONS else {})
