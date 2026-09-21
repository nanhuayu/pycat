"""On-demand archive derived view generation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pycat.core.content.archive_store import ArchivedContentRecord, SessionArchiveStore
from pycat.core.content.view_protocol import TOOL_SUMMARY_PROJECTION_CHARS
from pycat.core.state.operations import remember_archive
from pycat.models.conversation import Conversation, normalize_tool_result
from pycat.models.contracts.session_state import SessionState


@dataclass
class ArchiveViewResult:
    record: ArchivedContentRecord | None
    text: str = ""
    status: str = "pending"
    error: str = ""


class ArchiveViewService:
    def __init__(
        self,
        *,
        work_dir: str,
        conversation_id: object = None,
        conversation: Conversation | None = None,
        compressor: Any = None,
    ) -> None:
        self.store = SessionArchiveStore(work_dir, conversation_id=conversation_id, data_dir=getattr(conversation, "data_dir", None))
        self.conversation = conversation
        self.compressor = compressor

    async def get_or_create_summary(
        self,
        content_id: str,
        *,
        trace_purpose: str | None = None,
    ) -> ArchiveViewResult:
        record = self.store.read_record(content_id)
        if record is None:
            return ArchiveViewResult(record=None, status="not_found", error=f"Archived content not found: {content_id}")

        if record.summary:
            self.sync_state_and_messages(record)
            return ArchiveViewResult(record=record, text=record.summary, status="complete")

        if not self._can_generate_summary():
            self.sync_state_and_messages(record)
            return ArchiveViewResult(record=record, status=record.summary_status)

        fresh = self.store.read_record(record.id) or record
        if fresh.summary:
            self.sync_state_and_messages(fresh)
            return ArchiveViewResult(record=fresh, text=fresh.summary, status="complete")
        updated = await self._compress_record(
            fresh,
            trace_purpose=trace_purpose or "content_read:summary",
        )
        if updated is None:
            return ArchiveViewResult(
                record=None,
                status="not_found",
                error=f"Archived content disappeared during summary generation: {record.id}",
            )
        self.sync_state_and_messages(updated)
        return ArchiveViewResult(
            record=updated,
            text=updated.summary,
            status="complete" if updated.summary else updated.summary_status,
        )

    def _can_generate_summary(self) -> bool:
        return bool(self.compressor)

    async def _compress_record(
        self,
        record: ArchivedContentRecord,
        *,
        trace_purpose: str,
    ) -> ArchivedContentRecord | None:
        if not self._can_generate_summary():
            return record
        result = await self.compressor.compress(
            self.store.read_original(record),
            purpose="tool_result",
            images=self.store.read_images(record),
            conversation=self.conversation,
            trace_purpose=trace_purpose,
            content_id=record.id,
        )
        latest = self.store.read_record(record.id)
        if latest is None:
            return None
        if latest.summary:
            return latest
        return self._apply_summary(latest, result)

    def _apply_summary(
        self,
        record: ArchivedContentRecord,
        result: Any,
    ) -> ArchivedContentRecord:
        record.metadata = dict(record.metadata or {})
        record.metadata.update(
            {
                "compressed_token_estimate": int(getattr(result, "token_estimate", 0) or 0),
                "compression_calls": int(getattr(result, "calls", 0) or 0),
                "compression_chunks": int(getattr(result, "chunks", 0) or 0),
                "compression_reduce_levels": int(getattr(result, "reduce_levels", 0) or 0),
                "compression_strategy": str(getattr(result, "strategy", "") or ""),
                "compression_vision_fallback": bool(getattr(result, "vision_fallback", False)),
            }
        )
        summary = str(getattr(result, "summary", "") or "").strip()
        if summary:
            self.store.write_summary_view(
                record,
                summary=summary,
                source=f"capability__{getattr(result, 'capability_id', '') or 'compress'}",
                model=str(getattr(result, "model", "") or ""),
                metadata={
                    "fallback": getattr(result, "status", "") == "fallback",
                    "calls": int(getattr(result, "calls", 0) or 0),
                    "chunks": int(getattr(result, "chunks", 0) or 0),
                    "reduce_levels": int(getattr(result, "reduce_levels", 0) or 0),
                    "strategy": str(getattr(result, "strategy", "") or ""),
                    "vision_fallback": bool(getattr(result, "vision_fallback", False)),
                },
            )
        else:
            self.store.mark_summary_failed(
                record,
                error=str(getattr(result, "error", "") or getattr(result, "status", "") or "summary_unavailable"),
            )
        return self.store.read_record(record.id) or record

    def sync_state_and_messages(self, record: ArchivedContentRecord) -> None:
        if self.conversation is None or record is None:
            return
        try:
            state = self.conversation.get_state()
            if isinstance(state, SessionState) and record.kind == "tool_call":
                remember_archive(state, record)
                self.conversation.set_state(state)
        except Exception:
            pass
        self.sync_archive_result_metadata(self.conversation, record)

    @staticmethod
    def sync_archive_result_metadata(conversation: Conversation, record: ArchivedContentRecord) -> None:
        content_id = str(getattr(record, "id", "") or "")
        if not content_id:
            return
        for msg in getattr(conversation, "messages", []) or []:
            for tc in getattr(msg, "tool_calls", None) or []:
                if not isinstance(tc, dict) or tc.get("result") is None:
                    continue
                payload = normalize_tool_result(tc.get("result"))
                metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
                candidate = str(metadata.get("content_id") or metadata.get("archive_content_id") or "").strip()
                archive_record = metadata.get("archive_record")
                if candidate != content_id and isinstance(archive_record, dict):
                    if str(archive_record.get("id") or "") == content_id:
                        candidate = content_id
                if candidate != content_id:
                    continue
                metadata["content_id"] = content_id
                metadata["archive_size"] = int(getattr(record, "size", 0) or 0)
                metadata["archive_updated_seq"] = int(getattr(record, "updated_seq", 0) or getattr(record, "created_seq", 0) or 0)
                summary = str(
                    getattr(record, "summary", "")
                    or metadata.get("tool_result_summary")
                    or ""
                )[:TOOL_SUMMARY_PROJECTION_CHARS]
                metadata["tool_result_summary"] = summary
                metadata.pop("archive_record", None)
                payload["metadata"] = metadata
                if summary:
                    payload["summary"] = summary
                    tc["result_summary"] = summary
                tc["result"] = payload
                tc["result_metadata"] = dict(metadata)
