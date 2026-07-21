from __future__ import annotations

from typing import Any, Dict

from core.memory.service import MemoryService
from core.tools.base import BaseTool, ToolContext, ToolResult
from models.contracts.session_state import SessionState


class ManageMemoryTool(BaseTool):
    @property
    def name(self) -> str:
        return "state__memory"

    @property
    def display_name(self) -> str:
        return "管理记忆"

    @property
    def description(self) -> str:
        return "Read and curate reusable session, workspace, or global memory; durable candidates require explicit promotion."

    @property
    def category(self) -> str:
        return "state"

    def assess_risk(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        action = str(arguments.get("action") or "").lower()
        scope = str(arguments.get("scope") or "").lower()
        if action == "promote_candidate" and not scope:
            try:
                state = SessionState.from_dict(dict(context.state or {}))
                candidate = state.memory_candidates.get(str(arguments.get("candidate_id") or "").strip())
                scope = str(getattr(candidate, "scope", "") or "").lower()
            except Exception:
                scope = ""
        return "medium" if action in {"upsert", "delete", "promote_candidate"} and scope in {"workspace", "global"} else "low"

    def approval_message(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        return f"{str(arguments.get('action') or '').title()} {arguments.get('scope') or ''} memory {arguments.get('key') or ''}?"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list",
                        "view",
                        "upsert",
                        "delete",
                        "list_candidates",
                        "promote_candidate",
                        "reject_candidate",
                    ],
                },
                "scope": {"type": "string", "enum": ["session", "workspace", "global"]},
                "key": {"type": "string", "description": "Memory key for view, upsert, delete, or promotion override."},
                "content": {"type": "string", "description": "Concise reusable content for upsert."},
                "category": {
                    "type": "string",
                    "enum": ["preference", "fact", "decision", "convention", "command", "gotcha"],
                },
                "refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional supporting file, URL, archive, artifact, or tool-result references.",
                },
                "candidate_id": {"type": "string", "description": "Candidate id for promotion or rejection."},
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        action = str(arguments.get("action") or "").strip().lower()
        scope = str(arguments.get("scope") or "").strip().lower()
        scope = scope or "session"
        key = str(arguments.get("key") or "").strip()
        state = SessionState.from_dict(dict(context.state or {}))
        seq = int(context.state.get("_current_seq", 0) or 0)

        if action == "list":
            entries = MemoryService.list_memory_entries(state, scope=scope, work_dir=context.work_dir)
            if not entries:
                return ToolResult(f"No {scope} memory entries.")
            return ToolResult("\n".join(
                [f"{scope} memory:"]
                + [f"- {item['key']}" + (f" path={item['path']}" if item.get("path") else "") for item in entries]
            ))

        if action == "list_candidates":
            candidates = MemoryService.list_memory_candidates(state)
            if not candidates:
                return ToolResult("No pending memory candidates.")
            lines = ["Pending memory candidates:"]
            for item in candidates:
                refs = ", ".join(item.get("refs") or [])
                suffix = f" refs={refs}" if refs else ""
                lines.append(
                    f"- {item.get('id')} [{item.get('scope')}/{item.get('category')}]: "
                    f"{item.get('content')}{suffix}"
                )
            return ToolResult("\n".join(lines))

        if action == "promote_candidate":
            candidate_id = str(arguments.get("candidate_id") or "").strip()
            message = MemoryService.promote_memory_candidate(
                state,
                candidate_id=candidate_id,
                key=key,
                scope=str(arguments.get("scope") or "").strip().lower(),
                work_dir=context.work_dir,
                current_seq=seq,
            )
            self._sync(context, state, seq)
            return ToolResult(message, is_error=not candidate_id or "not found" in message.lower())

        if action == "reject_candidate":
            candidate_id = str(arguments.get("candidate_id") or "").strip()
            message = MemoryService.reject_memory_candidate(
                state,
                candidate_id=candidate_id,
                current_seq=seq,
            )
            self._sync(context, state, seq)
            return ToolResult(message, is_error=not candidate_id or "not found" in message.lower())

        if not key:
            return ToolResult("key is required for view, upsert, or delete.", is_error=True)
        if action == "view":
            content = MemoryService.read_memory_entry(state, scope=scope, key=key, work_dir=context.work_dir)
            return ToolResult(content) if content is not None else ToolResult(f"{scope} memory not found: {key}", is_error=True)
        if action == "upsert":
            content = str(arguments.get("content") or "")
            if not content.strip():
                return ToolResult("content is required for upsert.", is_error=True)
            message = MemoryService.write_memory_entry(
                state,
                scope=scope,
                key=key,
                content=content,
                work_dir=context.work_dir,
                current_seq=seq,
                category=str(arguments.get("category") or "fact"),
                refs=arguments.get("refs") if isinstance(arguments.get("refs"), list) else [],
            )
            self._sync(context, state, seq)
            failed = message.startswith("Failed") or "is too long" in message or message.endswith("is required.")
            return ToolResult(message, is_error=failed)
        if action == "delete":
            message = MemoryService.delete_memory_entry(state, scope=scope, key=key, work_dir=context.work_dir)
            self._sync(context, state, seq)
            return ToolResult(message)
        return ToolResult(f"Unknown memory action: {action}", is_error=True)

    @staticmethod
    def _sync(context: ToolContext, state: SessionState, seq: int) -> None:
        state.last_updated_seq = seq
        context.state.clear()
        context.state.update(state.to_dict())
        context.state["_current_seq"] = seq
