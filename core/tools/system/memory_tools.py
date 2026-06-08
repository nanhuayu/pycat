from __future__ import annotations

from typing import Any, Dict

from core.state.services.memory_service import MemoryService
from core.tools.base import BaseTool, ToolContext, ToolResult
from models.state import SessionState


class ManageMemoryTool(BaseTool):
    """Manage session, workspace, and global memory explicitly."""

    @property
    def name(self) -> str:
        return "state__memory"

    @property
    def description(self) -> str:
        return (
            "Manage PyCat memory across three explicit scopes: session, workspace, and global. "
            "Memory is for short durable reusable facts, preferences, verified commands, repo conventions, and gotchas. "
            "Do not store plans, reports, long tool outputs, transient todos, secrets, passwords, tokens, or unverified guesses.\n\n"
            "Scopes:\n"
            "- session: current conversation key-value facts stored in SessionState.memory.\n"
            "- workspace: project memory files under <work_dir>/.pycat/memory/ with MEMORY.md as the index.\n"
            "- global: user memory files under ~/.PyCat/memory/ with SOUL.md as the index.\n\n"
            "Important workflow:\n"
            "- Before creating or updating workspace/global memory, first use action=list or action=view to understand existing entries and avoid duplicates.\n"
            "- Prefer workspace memory for repo-specific conventions and verified commands.\n"
            "- Prefer global memory only for cross-workspace user preferences or reusable patterns.\n"
            "- Use session memory for temporary facts relevant only to this conversation.\n\n"
            "Actions:\n"
            "- list: list memory entries in a scope.\n"
            "- view: read one memory entry by key/path.\n"
            "- upsert: create or replace one memory entry.\n"
            "- delete: remove one memory entry.\n"
            "- list_candidates: list durable memory candidates extracted by runtime compress from archived content.\n"
            "- promote_candidate: explicitly save one candidate as memory.\n"
            "- reject_candidate: hide one candidate."
        )

    @property
    def category(self) -> str:
        return "manage"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "view", "upsert", "delete", "list_candidates", "promote_candidate", "reject_candidate"],
                    "description": "Memory operation to perform.",
                },
                "scope": {
                    "type": "string",
                    "enum": ["session", "workspace", "global"],
                    "description": "Memory scope. Default: session.",
                },
                "key": {
                    "type": "string",
                    "description": "Memory key or relative memory file path. Required for view/upsert/delete.",
                },
                "content": {
                    "type": "string",
                    "description": "Memory body for action=upsert. Keep it concise and durable.",
                },
                "reason": {
                    "type": "string",
                    "description": "Why this memory should be stored or changed.",
                },
                "category": {
                    "type": "string",
                    "enum": ["preference", "fact", "decision", "convention", "command", "gotcha"],
                    "description": "Durable memory category. Default: fact.",
                },
                "evidence_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional source paths, URLs, or archive content IDs supporting this memory.",
                },
                "confidence": {
                    "type": "number",
                    "description": "Optional confidence from 0 to 1. Default: 1.",
                },
                "candidate_id": {
                    "type": "string",
                    "description": "Candidate id for promote_candidate/reject_candidate.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum candidates to list. Default: 20.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags written to file memory frontmatter.",
                },
            },
            "required": ["action"],
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        action = str(arguments.get("action") or "").strip().lower()
        scope = str(arguments.get("scope") or "session").strip().lower()
        key = str(arguments.get("key") or "").strip()
        content = str(arguments.get("content") or "")
        reason = str(arguments.get("reason") or "").strip()
        category = str(arguments.get("category") or "fact").strip().lower()
        evidence_refs = arguments.get("evidence_refs") or []
        confidence = float(arguments.get("confidence") or 1.0)
        tags = arguments.get("tags") or []
        if scope not in MemoryService.SOURCE_OPTIONS:
            return ToolResult(f"Unknown memory scope: {scope}", is_error=True)

        state_dict = context.state if context.state is not None else {}
        state = SessionState.from_dict(state_dict)
        current_seq = int(state_dict.get("_current_seq", 0) or 0)

        if action == "list_candidates":
            candidates = MemoryService.list_memory_candidates(state, limit=int(arguments.get("limit") or 20))
            if not candidates:
                return ToolResult("No memory candidates.")
            lines = ["Memory candidates:"]
            for item in candidates:
                refs = ",".join((item.get("evidence_refs") or [])[:3])
                refs_text = f" refs={refs}" if refs else ""
                lines.append(
                    f"- {item.get('id')} [{item.get('category')}] confidence={item.get('confidence')}: "
                    f"{item.get('content')}{refs_text}"
                )
            return ToolResult("\n".join(lines))

        if action == "promote_candidate":
            message = MemoryService.promote_memory_candidate(
                state,
                candidate_id=str(arguments.get("candidate_id") or "").strip(),
                key=key,
                scope=scope,
                work_dir=context.work_dir,
                current_seq=current_seq,
            )
            self._sync_context_state(context.state, state, current_seq)
            return ToolResult(message)

        if action == "reject_candidate":
            message = MemoryService.reject_memory_candidate(
                state,
                candidate_id=str(arguments.get("candidate_id") or "").strip(),
            )
            self._sync_context_state(context.state, state, current_seq)
            return ToolResult(message)

        if action == "list":
            entries = MemoryService.list_memory_entries(state, scope=scope, work_dir=context.work_dir)
            if not entries:
                return ToolResult(f"No {scope} memory entries.")
            lines = [f"{scope} memory entries:"]
            for item in entries:
                updated = f" updated={item.get('updated')}" if item.get("updated") else ""
                path = f" path={item.get('path')}" if item.get("path") else ""
                lines.append(f"- {item.get('key')}{path}{updated}")
            return ToolResult("\n".join(lines))

        if action in {"view", "upsert", "delete"} and not key:
            return ToolResult("key is required for view/upsert/delete.", is_error=True)

        if action == "view":
            viewed = MemoryService.read_memory_entry(state, scope=scope, key=key, work_dir=context.work_dir)
            if viewed is None:
                return ToolResult(f"{scope} memory not found: {key}", is_error=True)
            return ToolResult(viewed)

        if action == "upsert":
            if not content.strip():
                return ToolResult("content is required for action=upsert.", is_error=True)
            message = MemoryService.write_memory_entry(
                state,
                scope=scope,
                key=key,
                content=content,
                work_dir=context.work_dir,
                current_seq=current_seq,
                reason=reason,
                tags=tags if isinstance(tags, list) else [],
                category=category,
                evidence_refs=evidence_refs if isinstance(evidence_refs, list) else [],
                confidence=confidence,
            )
            self._sync_context_state(context.state, state, current_seq)
            return ToolResult(message)

        if action == "delete":
            message = MemoryService.delete_memory_entry(state, scope=scope, key=key, work_dir=context.work_dir)
            self._sync_context_state(context.state, state, current_seq)
            return ToolResult(message)

        return ToolResult(f"Unknown action: {action}", is_error=True)

    @staticmethod
    def _sync_context_state(context_state: Dict[str, object], state: SessionState, current_seq: int) -> None:
        state.last_updated_seq = current_seq
        updated = state.to_dict()
        context_state.clear()
        context_state.update(updated)
        context_state["_current_seq"] = current_seq
