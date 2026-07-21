from __future__ import annotations

import re
from typing import Any, Dict

from core.content.archive_store import SessionArchiveStore, normalize_archive_kind
from core.content.archive_view_service import ArchiveViewService
from core.context.compression import MIN_LLM_COMPRESSION_CHARS
from core.tools.base import BaseTool, ToolContext, ToolResult


ARCHIVE_CONTENT_CHARS = 8000


def _session_ids(context: ToolContext) -> list[str | None]:
    values: list[str | None] = []
    current = getattr(getattr(context, "conversation", None), "id", None)
    if current:
        values.append(str(current))
    settings = getattr(getattr(context, "conversation", None), "settings", {}) or {}
    parent = settings.get("parent_session_id") if isinstance(settings, dict) else None
    if parent and str(parent) not in values:
        values.append(str(parent))
    return values or [None]


def _find_record(context: ToolContext, content_id: str):
    for session_id in _session_ids(context):
        store = SessionArchiveStore(context.work_dir, conversation_id=session_id)
        record = store.read_record(content_id)
        if record is not None:
            return store, record, session_id
    return None, None, None


class ArchiveListTool(BaseTool):
    @property
    def name(self) -> str:
        return "archive__list"

    @property
    def display_name(self) -> str:
        return "列出会话归档"

    @property
    def description(self) -> str:
        return "List archived tool or history content for the current session."

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
                    "description": "Optional archive kind.",
                },
                "limit": {"type": "integer", "description": "Maximum records; default 20, max 50."},
            },
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            limit = max(1, min(int(arguments.get("limit") or 20), 50))
            kind = normalize_archive_kind(arguments.get("kind")) if arguments.get("kind") else None
        except Exception as exc:
            return ToolResult(f"Invalid argument: {exc}", is_error=True)
        records = []
        seen: set[str] = set()
        for session_id in _session_ids(context):
            store = SessionArchiveStore(context.work_dir, conversation_id=session_id)
            for record in store.list_records(kind=kind, limit=limit):
                if record.id in seen:
                    continue
                seen.add(record.id)
                records.append(record)
                if len(records) >= limit:
                    break
        if not records:
            return ToolResult("No archived content in this session.")
        lines = [
            "Archived content:",
            'Use archive__read(content_id="...", view="content", offset=0) to restore exact text.',
        ]
        for record in records:
            line = f"- content_id={record.id} source={record.source or '-'} chars={record.size}"
            if record.summary:
                line += f" summary={' '.join(record.summary.split())[:360]}"
            lines.append(line)
        return ToolResult("\n".join(lines))


class ArchiveReadTool(BaseTool):
    @property
    def name(self) -> str:
        return "archive__read"

    @property
    def display_name(self) -> str:
        return "读取会话归档"

    @property
    def description(self) -> str:
        return "Read a fixed summary or an exact 8000-character chunk of archived session content."

    @property
    def category(self) -> str:
        return "read"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content_id": {"type": "string", "description": "Identifier from archive__list or archive index."},
                "view": {
                    "type": "string",
                    "enum": ["summary", "content"],
                    "description": "Derived summary or exact content chunk; default summary.",
                },
                "offset": {"type": "integer", "description": "0-based character offset for view=content."},
            },
            "required": ["content_id"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        content_id = str(arguments.get("content_id") or "").strip()
        view = str(arguments.get("view") or "summary").strip().lower()
        if not content_id:
            return ToolResult("content_id is required.", is_error=True)
        if view not in {"summary", "content"}:
            return ToolResult("view must be summary or content.", is_error=True)
        store, record, session_id = _find_record(context, content_id)
        if store is None or record is None:
            return ToolResult(f"Archived content not found: {content_id}", is_error=True)

        if view == "summary":
            try:
                exact = store.read_original(record)
                images = store.read_images(record)
            except Exception as exc:
                return ToolResult(f"Archive read error: {exc}", is_error=True)
            if len(exact) < MIN_LLM_COMPRESSION_CHARS and not images:
                return self._result_with_images(
                    f"[content:0-{len(exact)}]\ncontent_id={content_id}\nexact=true\nnext_offset=none\n{exact}"
                )
            service = ArchiveViewService(
                work_dir=context.work_dir,
                conversation_id=session_id,
                conversation=getattr(context, "conversation", None),
                compressor=self._compressor(context, store),
            )
            result = await service.get_or_create_summary(
                content_id,
            )
            if result.text:
                self._sync_state(context)
                return ToolResult(f"[summary]\ncontent_id={content_id}\n{result.text}")
            end = min(len(exact), ARCHIVE_CONTENT_CHARS)
            next_offset = end if end < len(exact) else None
            return self._result_with_images(
                f"[content:0-{end}]\ncontent_id={content_id}\nexact=true\n"
                f"next_offset={next_offset if next_offset is not None else 'none'}\n{exact[:end]}",
                images,
            )

        try:
            text = store.read_original(record)
            offset = max(0, int(arguments.get("offset") or 0))
        except Exception as exc:
            return ToolResult(f"Archive read error: {exc}", is_error=True)
        offset = min(offset, len(text))
        end = min(len(text), offset + ARCHIVE_CONTENT_CHARS)
        next_offset = end if end < len(text) else None
        rendered = (
            f"[content:{offset}-{end}]\ncontent_id={content_id}\nexact=true\n"
            f"next_offset={next_offset if next_offset is not None else 'none'}\n{text[offset:end]}"
        )
        return self._result_with_images(rendered, store.read_images(record) if offset == 0 else [])

    @staticmethod
    def _result_with_images(text: str, images: list[str] | None = None) -> ToolResult:
        if not images:
            return ToolResult(text)
        blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for image in images:
            value = str(image or "").strip()
            if not value:
                continue
            match = re.match(r"^data:([^;,]+);base64,", value, flags=re.IGNORECASE)
            mime_type = str(match.group(1) if match else "image/png")
            blocks.append({"type": "image", "mimeType": mime_type, "data": value})
        return ToolResult(blocks)

    @staticmethod
    def _compressor(context: ToolContext, store: SessionArchiveStore):
        runtime = getattr(context, "runtime", None)
        factory = getattr(runtime, "archive_compressor_factory", None)
        if factory is None:
            return None
        return factory(
            client=getattr(context, "llm_client", None),
            provider=getattr(context, "provider", None),
            store=store,
            debug_trace=getattr(runtime, "debug_trace", None),
        )

    @staticmethod
    def _sync_state(context: ToolContext) -> None:
        try:
            current_seq = int(context.state.get("_current_seq", 0) or 0)
            state = context.conversation.get_state().to_dict()
            context.state.clear()
            context.state.update(state)
            context.state["_current_seq"] = current_seq
        except Exception:
            return
