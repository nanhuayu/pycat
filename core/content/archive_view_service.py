"""On-demand archive derived view generation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from core.content.archive_store import ArchivedContentRecord, SessionArchiveStore
from core.state.operations import remember_archive
from models.conversation import Conversation, normalize_tool_result
from models.contracts.session_state import SessionState


@dataclass
class ArchiveViewResult:
    record: ArchivedContentRecord | None
    text: str = ""
    status: str = "pending"
    error: str = ""


class ArchiveViewService:
    _locks: dict[tuple[str, str], asyncio.Lock] = {}

    def __init__(
        self,
        *,
        work_dir: str,
        conversation_id: object = None,
        conversation: Conversation | None = None,
        compressor: Any = None,
    ) -> None:
        self.store = SessionArchiveStore(work_dir, conversation_id=conversation_id)
        self.conversation = conversation
        self.compressor = compressor

    async def get_or_create_summary(
        self,
        content_id: str,
        *,
        purpose: str | None = None,
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

        lock_key = (str(self.store.session_root), str(record.id))
        lock = self._locks.setdefault(lock_key, asyncio.Lock())

        async def _work() -> ArchiveViewResult:
            async with lock:
                fresh = self.store.read_record(record.id) or record
                if fresh.summary:
                    self.sync_state_and_messages(fresh)
                    return ArchiveViewResult(record=fresh, text=fresh.summary, status="complete")
                updated = await self._compress_record(fresh, purpose=purpose or "content_read:summary")
                self.sync_state_and_messages(updated)
                return ArchiveViewResult(
                    record=updated,
                    text=updated.summary,
                    status="complete" if updated.summary else updated.summary_status,
                )

        return await _work()

    def _can_generate_summary(self) -> bool:
        return bool(self.compressor)

    async def _compress_record(self, record: ArchivedContentRecord, *, purpose: str) -> ArchivedContentRecord:
        if not self._can_generate_summary():
            return record
        result = await self.compressor.summarize_archive(record, conversation=self.conversation, purpose=purpose)
        return self.compressor.apply_archive_summary(record, result, conversation=self.conversation)

    def sync_state_and_messages(self, record: ArchivedContentRecord) -> None:
        if self.conversation is None or record is None:
            return
        try:
            state = self.conversation.get_state()
            if isinstance(state, SessionState):
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
                metadata["tool_result_summary"] = str(getattr(record, "summary", "") or metadata.get("tool_result_summary") or "")
                metadata.pop("archive_record", None)
                payload["metadata"] = metadata
                if getattr(record, "summary", ""):
                    payload["summary"] = str(record.summary)
                    tc["result_summary"] = str(record.summary)
                tc["result"] = payload
                tc["result_metadata"] = dict(metadata)
