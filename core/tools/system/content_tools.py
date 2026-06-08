from __future__ import annotations

from typing import Any, Dict

from core.context.archive_view_service import ArchiveViewService, normalize_summary_mode
from core.context.archive_store import SessionArchiveStore, normalize_archive_kind
from core.content.view_protocol import ContentViewLabel, char_view_value, line_view_value, summary_view_value
from core.tools.base import BaseTool, ToolContext, ToolResult


class ContentListTool(BaseTool):
    @property
    def name(self) -> str:
        return "content__list"

    @property
    def description(self) -> str:
        return (
            "List PyCat archived content for the current session. "
            "Use this for tool-call/history archive indexes, not for workspace files."
        )

    @property
    def category(self) -> str:
        return "read"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["tool_call", "history", "artifact"],
                    "description": "Optional archive kind filter.",
                },
                "limit": {"type": "integer", "description": "Maximum records to list. Default: 20."},
            },
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        kind = arguments.get("kind")
        limit = int(arguments.get("limit") or 20)
        limit = max(1, min(limit, 100))
        store = SessionArchiveStore(context.work_dir, conversation_id=getattr(context.conversation, "id", None))
        records = store.list_records(kind=normalize_archive_kind(kind) if kind else None, limit=limit)
        if not records:
            return ToolResult("No archived content in this session.")
        lines = ["Archived content:"]
        for record in records:
            views = ["full"]
            if record.summary:
                views.append("summary")
            else:
                views.append("summary:pending" if record.summary_status == "pending" else "summary:failed")
            lines.append(
                f"- {record.id} kind={record.kind} source={record.source or '-'} "
                f"chars={record.size} digest={record.digest[:16]} views={','.join(views)} "
                f"title=\"{record.title}\""
            )
        return ToolResult("\n".join(lines))


class ContentReadTool(BaseTool):
    @property
    def name(self) -> str:
        return "content__read"

    @property
    def description(self) -> str:
        return (
            "Read PyCat archived content by content_id. This reads tool-call/history archive records, "
            "not workspace files. Use file__read for real files. Views: summary, full, lines, chars. "
            "For view=summary the runtime waits for or creates the internal compress view by default. "
            "Responses use [type] or [type:desc] labels: [summary], [summary:detailed], [summary:topic], "
            "[summary:pending], [full], [line:1-200], or [char:0-4000]."
        )

    @property
    def category(self) -> str:
        return "read"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content_id": {"type": "string", "description": "Archived content id from content__list or <archive_index>."},
                "view": {
                    "type": "string",
                    "enum": ["summary", "full", "lines", "chars"],
                    "description": "Read view. Default: summary.",
                },
                "start_line": {"type": "integer", "description": "1-based start line for view=lines."},
                "end_line": {"type": "integer", "description": "1-based end line for view=lines."},
                "char_start": {"type": "integer", "description": "0-based start char for view=chars."},
                "char_end": {"type": "integer", "description": "0-based end char for view=chars."},
                "summary_mode": {
                    "type": "string",
                    "enum": ["balanced", "brief", "detailed", "timeline", "evidence", "topic", "memory_candidates"],
                    "description": "Summary style for view=summary. Default: balanced.",
                },
                "topic": {
                    "type": "string",
                    "description": "Optional topic for summary_mode=topic or evidence.",
                },
                "wait": {
                    "type": "boolean",
                    "description": "For view=summary, wait for runtime compression. Default: true.",
                },
                "timeout_ms": {
                    "type": "integer",
                    "description": "Maximum wait for summary generation. Default: 15000.",
                },
                "refresh": {
                    "type": "boolean",
                    "description": "Regenerate the requested summary view. Default: false.",
                },
            },
            "required": ["content_id"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        content_id = str(arguments.get("content_id") or "").strip()
        view = str(arguments.get("view") or "summary").strip().lower()
        if not content_id:
            return ToolResult("content_id is required.", is_error=True)
        store = SessionArchiveStore(context.work_dir, conversation_id=getattr(context.conversation, "id", None))
        record = store.read_record(content_id)
        if record is None:
            return ToolResult(f"Archived content not found: {content_id}", is_error=True)

        if view == "summary":
            wait = bool(arguments.get("wait", True))
            mode = normalize_summary_mode(arguments.get("summary_mode") or "balanced")
            topic = str(arguments.get("topic") or "").strip()
            timeout_ms = int(arguments.get("timeout_ms") or 15_000)
            timeout_ms = max(500, min(timeout_ms, 120_000))
            refresh = bool(arguments.get("refresh", False))
            service = ArchiveViewService(
                work_dir=context.work_dir,
                conversation_id=getattr(context.conversation, "id", None),
                client=getattr(context, "llm_client", None),
                provider=getattr(context, "provider", None),
                conversation=getattr(context, "conversation", None),
            )
            result = await service.get_or_create_summary(
                content_id,
                mode=mode,
                topic=topic,
                wait=wait,
                timeout_ms=timeout_ms,
                refresh=refresh,
            )
            if result.record is None:
                return ToolResult(result.error or f"Archived content not found: {content_id}", is_error=True)
            self._sync_context_state(context)
            if result.text:
                topic_line = f"\ntopic={topic}" if topic else ""
                return ToolResult(f"{ContentViewLabel.parse(result.label).bracketed}\ncontent_id={result.record.id}{topic_line}\n{result.text}")
            status = result.status if result.status in {"pending", "failed"} else "pending"
            return ToolResult(
                f"{ContentViewLabel.parse(summary_view_value(status)).bracketed}\n"
                f"content_id={result.record.id}\n"
                f"Summary is not ready. Use content__read(view=\"full\"), view=\"lines\", or view=\"chars\" for exact archived content."
            )

        try:
            text = store.read_original(record)
        except Exception as exc:
            return ToolResult(f"Failed to read archived original: {exc}", is_error=True)

        if view == "lines":
            lines = text.splitlines()
            total = len(lines)
            start = max(1, int(arguments.get("start_line") or 1))
            end = min(total, int(arguments.get("end_line") or min(total, start + 199)))
            if start > end:
                return ToolResult(f"Invalid line range: {start}-{end}", is_error=True)
            body = "\n".join(lines[start - 1:end])
            return ToolResult(f"{ContentViewLabel.parse(line_view_value(start, end)).bracketed}\ncontent_id={record.id}\nlines={start}-{end} of {total}\n{body}")

        if view == "chars":
            total = len(text)
            start = max(0, int(arguments.get("char_start") or 0))
            end = min(total, int(arguments.get("char_end") or min(total, start + 4000)))
            if start > end:
                return ToolResult(f"Invalid char range: {start}-{end}", is_error=True)
            return ToolResult(f"{ContentViewLabel.parse(char_view_value(start, end)).bracketed}\ncontent_id={record.id}\nchars={start}-{end} of {total}\n{text[start:end]}")

        if view != "full":
            return ToolResult(f"Unknown view: {view}", is_error=True)
        return ToolResult(f"{ContentViewLabel.build('full')}\ncontent_id={record.id}\n{text}")

    @staticmethod
    def _sync_context_state(context: ToolContext) -> None:
        try:
            if context.conversation is None or context.state is None:
                return
            state = context.conversation.get_state()
            current_seq = context.state.get("_current_seq", 0) if isinstance(context.state, dict) else 0
            context.state.clear()
            context.state.update(state.to_dict())
            context.state["_current_seq"] = current_seq
        except Exception:
            return
