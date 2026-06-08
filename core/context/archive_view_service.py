"""On-demand archive derived view generation."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any

from core.context.archive_store import ArchivedContentRecord, SessionArchiveStore
from core.content.view_protocol import normalize_summary_mode, summary_view_value
from models.conversation import Conversation, normalize_tool_result
from models.provider import Provider
from models.state import SessionState


@dataclass
class ArchiveViewResult:
    record: ArchivedContentRecord | None
    text: str = ""
    label: str = "summary"
    status: str = "pending"
    error: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.text)


class ArchiveViewService:
    _locks: dict[tuple[str, str], asyncio.Lock] = {}

    def __init__(
        self,
        *,
        work_dir: str,
        conversation_id: object = None,
        client: Any = None,
        provider: Provider | None = None,
        conversation: Conversation | None = None,
    ) -> None:
        self.store = SessionArchiveStore(work_dir, conversation_id=conversation_id)
        self.client = client
        self.provider = provider
        self.conversation = conversation

    async def get_or_create_summary(
        self,
        content_id: str,
        *,
        mode: str = "balanced",
        topic: str = "",
        wait: bool = True,
        timeout_ms: int = 15_000,
        refresh: bool = False,
        purpose: str | None = None,
    ) -> ArchiveViewResult:
        normalized_mode = normalize_summary_mode(mode)
        record = self.store.read_record(content_id)
        if record is None:
            return ArchiveViewResult(record=None, status="not_found", error=f"Archived content not found: {content_id}")

        view_key = self.view_key(normalized_mode, topic)
        label = self.view_label(normalized_mode)
        text = self._existing_view_text(record, normalized_mode, view_key)
        if text and not refresh:
            self.sync_state_and_messages(record)
            return ArchiveViewResult(record=record, text=text, label=label, status="complete")

        if not wait or not self.client or not self.provider:
            self.sync_state_and_messages(record)
            return ArchiveViewResult(record=record, text="", label=label, status=record.summary_status)

        lock_key = (str(self.store.session_root), str(record.id))
        lock = self._locks.setdefault(lock_key, asyncio.Lock())

        async def _work() -> ArchiveViewResult:
            async with lock:
                fresh = self.store.read_record(record.id) or record
                text = self._existing_view_text(fresh, normalized_mode, view_key)
                if text and not refresh:
                    self.sync_state_and_messages(fresh)
                    return ArchiveViewResult(record=fresh, text=text, label=label, status="complete")
                updated = await self._compress_record(fresh, purpose=purpose or f"content_read:{normalized_mode}")
                text = self._existing_view_text(updated, normalized_mode, view_key)
                if not text and normalized_mode in {"topic", "evidence"} and topic:
                    text = await self._write_topic_view(updated, normalized_mode, topic, view_key)
                    updated = self.store.read_record(updated.id) or updated
                self.sync_state_and_messages(updated)
                return ArchiveViewResult(
                    record=updated,
                    text=text,
                    label=label,
                    status="complete" if text else updated.summary_status,
                )

        try:
            return await asyncio.wait_for(_work(), timeout=max(0.1, int(timeout_ms or 15_000) / 1000))
        except asyncio.TimeoutError:
            latest = self.store.read_record(record.id) or record
            self.sync_state_and_messages(latest)
            return ArchiveViewResult(record=latest, text="", label=label, status="pending", error="summary_timeout")

    async def ensure_summary(self, record: ArchivedContentRecord, *, purpose: str = "maintenance") -> ArchivedContentRecord:
        result = await self.get_or_create_summary(
            record.id,
            mode="balanced",
            wait=bool(self.client and self.provider),
            timeout_ms=60_000,
            refresh=False,
            purpose=purpose,
        )
        return result.record or record

    async def _compress_record(self, record: ArchivedContentRecord, *, purpose: str) -> ArchivedContentRecord:
        if not self.client or not self.provider:
            return record
        from core.context.capability_compression import CapabilityCompressionOrchestrator

        orchestrator = CapabilityCompressionOrchestrator(self.client, self.provider, store=self.store)
        result = await orchestrator.summarize_archive(record, conversation=self.conversation, purpose=purpose)
        return orchestrator.apply_archive_summary(record, result, conversation=self.conversation)

    async def _write_topic_view(self, record: ArchivedContentRecord, mode: str, topic: str, view_key: str) -> str:
        if not self.client or not self.provider:
            return ""
        from core.context.capability_compression import CapabilityCompressionOrchestrator

        orchestrator = CapabilityCompressionOrchestrator(self.client, self.provider, store=self.store)
        result = await orchestrator.summarize_archive_topic(
            record,
            mode=mode,
            topic=topic,
            conversation=self.conversation,
        )
        text = result.summary_detailed or result.summary or result.summary_brief
        if text:
            self.store.write_view(
                record,
                key=view_key,
                label=self.view_label(mode),
                text=text,
                source=f"capability__{result.capability_id or 'compress'}",
                model=result.model,
                confidence=result.confidence,
                metadata={"topic": topic, "mode": mode},
            )
        return text

    def sync_state_and_messages(self, record: ArchivedContentRecord) -> None:
        if self.conversation is None or record is None:
            return
        try:
            state = self.conversation.get_state()
            if isinstance(state, SessionState):
                state.remember_archive(record)
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
                metadata["archive_ref"] = str(getattr(record, "original_ref", "") or "")
                metadata["archive_status"] = str(getattr(record, "status", "") or "")
                metadata["tool_result_summary"] = str(getattr(record, "summary", "") or metadata.get("tool_result_summary") or "")
                metadata["tool_result_summary_status"] = str(getattr(record, "summary_status", "") or "pending")
                metadata["tool_result_compression_required"] = not bool(getattr(record, "summary", ""))
                metadata["archive_record"] = record.to_dict()
                payload["metadata"] = metadata
                if getattr(record, "summary", ""):
                    payload["summary"] = str(record.summary)
                    tc["result_summary"] = str(record.summary)
                tc["result"] = payload
                tc["result_metadata"] = dict(metadata)

    @staticmethod
    def view_key(mode: str, topic: str = "") -> str:
        normalized = normalize_summary_mode(mode)
        if normalized == "balanced":
            return "summary"
        if normalized in {"topic", "evidence"}:
            digest = hashlib.sha1(str(topic or "").encode("utf-8", errors="replace")).hexdigest()[:10]
            return f"summary.{normalized}.{digest}"
        return f"summary.{normalized}"

    @staticmethod
    def view_label(mode: str) -> str:
        return summary_view_value(mode)

    @staticmethod
    def _existing_view_text(record: ArchivedContentRecord, mode: str, view_key: str) -> str:
        if mode == "balanced":
            return record.summary
        text = record.view_text(view_key)
        if text:
            return text
        if mode == "brief":
            return record.view_text("summary.brief") or record.summary
        if mode == "detailed":
            return record.view_text("summary.detailed") or record.summary
        if mode == "timeline":
            return record.view_text("summary.timeline")
        if mode == "memory_candidates":
            return record.view_text("summary.memory_candidates")
        return ""
