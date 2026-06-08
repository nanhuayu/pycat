"""Progressive session context maintenance for archive-first PyCat state."""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass

from core.context.archive_view_service import ArchiveViewService
from core.context.archive_store import SessionArchiveStore
from core.context.capability_compression import CapabilityCompressionOrchestrator
from core.llm.token_budget import estimate_conversation_tokens
from core.prompts.history import count_user_turn_blocks, is_control_message
from core.prompts.user_context import extract_user_request
from models.conversation import Conversation, Message, normalize_tool_result, tool_call_name
from models.provider import Provider
from models.state import SessionState


@dataclass(frozen=True)
class MaintenancePolicy:
    keep_last_turns: int = 3
    soft_turns: int = 4
    max_summary_chars: int = 12_000
    token_soft_ratio: float = 0.35
    token_hard_ratio: float = 0.70


@dataclass(frozen=True)
class MaintenanceReport:
    summarized_messages: int = 0
    archived_messages: int = 0
    memory_updates: int = 0
    summary_updated: bool = False
    reason: str = ""
    snipped_messages: int = 0
    archive_updates: int = 0


class ContextMaintenanceService:
    """Runtime cleaner for summary, work trace, archive views, and memory."""

    TRANSIENT_MEMORY_KEYS = {"context.diagnostics"}
    TRANSIENT_MEMORY_PREFIXES = ("auto.fact.",)

    def __init__(self, policy: MaintenancePolicy | None = None) -> None:
        self.policy = policy or MaintenancePolicy()

    def maintain(
        self,
        conversation: Conversation,
        *,
        context_window_limit: int = 0,
        current_seq: int = 0,
        force: bool = False,
    ) -> MaintenanceReport:
        try:
            return asyncio.run(
                self.maintain_async(
                    conversation,
                    client=None,
                    provider=None,
                    context_window_limit=context_window_limit,
                    current_seq=current_seq,
                    force=force,
                )
            )
        except RuntimeError:
            # Fallback for rare nested-loop environments: run the sync path
            return self._maintain_sync(
                conversation,
                context_window_limit=context_window_limit,
                current_seq=current_seq,
                force=force,
            )

    async def maintain_async(
        self,
        conversation: Conversation,
        *,
        client,
        provider: Provider | None,
        context_window_limit: int = 0,
        current_seq: int = 0,
        force: bool = False,
    ) -> MaintenanceReport:
        return await self._maintain_impl(
            conversation,
            client=client,
            provider=provider,
            context_window_limit=context_window_limit,
            current_seq=current_seq,
            force=force,
        )

    def _maintain_sync(
        self,
        conversation: Conversation,
        *,
        context_window_limit: int = 0,
        current_seq: int = 0,
        force: bool = False,
    ) -> MaintenanceReport:
        return asyncio.get_event_loop().run_until_complete(
            self._maintain_impl(
                conversation,
                client=None,
                provider=None,
                context_window_limit=context_window_limit,
                current_seq=current_seq,
                force=force,
            )
        )

    async def _maintain_impl(
        self,
        conversation: Conversation,
        *,
        client,
        provider: Provider | None,
        context_window_limit: int = 0,
        current_seq: int = 0,
        force: bool = False,
    ) -> MaintenanceReport:
        state = conversation.get_state()
        last_seq = int(getattr(state, "last_maintenance_seq", 0) or 0)
        latest_seq = self._latest_seq(conversation)
        if not force and latest_seq <= last_seq:
            return MaintenanceReport(reason="up_to_date")

        snipped = self._snip_zombie_messages(conversation)
        memory_updates = self._clean_transient_memory(state)
        archive_updates = 0

        if client and provider:
            archive_updates = await self._compress_pending_archives_async(
                conversation,
                state,
                client=client,
                provider=provider,
            )

        should_archive, reason = self._should_archive(conversation, context_window_limit, force=force)
        summary_updated = False
        if (should_archive or not str(state.summary or "").strip()) and client and provider:
            orchestrator = CapabilityCompressionOrchestrator(client, provider)
            history_result = await orchestrator.compress_history(conversation.messages, state, conversation=conversation)
            if history_result.summary:
                state.summary = history_result.summary[: self.policy.max_summary_chars]
                summary_updated = True
        if should_archive:
            archived = self._archive_old_turns(conversation, state)
        else:
            archived = 0

        state.summary = self._sanitize_state_summary(str(state.summary or ""))
        state.last_maintenance_seq = max(latest_seq, int(current_seq or 0), last_seq)
        if snipped or memory_updates or archive_updates or summary_updated or archived:
            state.state_version += 1
            state.last_updated_seq = max(state.last_updated_seq, state.last_maintenance_seq)
        conversation.set_state(state)
        return MaintenanceReport(
            summarized_messages=1 if summary_updated else 0,
            archived_messages=archived,
            memory_updates=memory_updates,
            summary_updated=summary_updated,
            reason=reason,
            snipped_messages=snipped,
            archive_updates=archive_updates,
        )

    @staticmethod
    def _snip_zombie_messages(conversation: Conversation) -> int:
        messages = list(getattr(conversation, "messages", []) or [])
        if len(messages) <= 8:
            return 0
        keep_tail = messages[-6:]
        tail_ids = {id(msg) for msg in keep_tail}
        kept: list[Message] = []
        removed = 0
        for msg in messages:
            if id(msg) in tail_ids or getattr(msg, "role", "") == "system":
                kept.append(msg)
                continue
            if getattr(msg, "condense_parent", None) or getattr(msg, "truncation_parent", None):
                removed += 1
                continue
            kept.append(msg)
        if removed:
            conversation.messages = kept
        return removed

    async def _compress_pending_archives_async(
        self,
        conversation: Conversation,
        state: SessionState,
        *,
        client,
        provider: Provider,
    ) -> int:
        if not state.archive_index:
            return 0
        service = ArchiveViewService(
            work_dir=getattr(conversation, "work_dir", "") or ".",
            conversation_id=getattr(conversation, "id", None),
            client=client,
            provider=provider,
            conversation=conversation,
        )
        updates = 0
        for content_id, record in list(state.archive_index.items()):
            metadata = dict(getattr(record, "metadata", {}) or {})
            status = str(getattr(record, "status", "") or metadata.get("summary_status") or "").strip().lower()
            needs_compression = not str(getattr(record, "summary", "") or "").strip() or status in {"", "pending", "summary_pending", "summary_failed", "fallback"}
            if not needs_compression:
                continue
            updated = await service.ensure_summary(record, purpose="context_maintenance")
            state.archive_index[content_id] = updated
            ArchiveViewService.sync_archive_result_metadata(conversation, updated)
            updates += 1
        return updates

    @staticmethod
    def _sync_archive_result_metadata(conversation: Conversation, record) -> None:
        ArchiveViewService.sync_archive_result_metadata(conversation, record)

    def _should_archive(
        self,
        conversation: Conversation,
        context_window_limit: int,
        *,
        force: bool,
    ) -> tuple[bool, str]:
        if force:
            return True, "force"
        turn_blocks = count_user_turn_blocks(conversation.messages)
        if turn_blocks > max(2, self.policy.keep_last_turns):
            return True, "progressive_turn_growth"
        if turn_blocks > self.policy.soft_turns:
            return True, "soft_turn_growth"
        if context_window_limit > 0:
            tokens = estimate_conversation_tokens(conversation)
            if tokens > int(context_window_limit * self.policy.token_soft_ratio):
                return True, "soft_token_growth"
        return False, "incremental_only"

    def _archive_old_turns(self, conversation: Conversation, state: SessionState) -> int:
        candidates = self._messages_to_archive(conversation)
        if not candidates:
            return 0
        max_seq = max(int(getattr(msg, "seq_id", 0) or 0) for msg in candidates)
        try:
            state.work_trace.archive_through(max_seq)
        except Exception:
            pass
        state.archived_summaries = list(state.archived_summaries or [])
        if len(state.archived_summaries) > 12:
            state.archived_summaries = state.archived_summaries[-12:]
        return len(candidates)

    def _messages_to_archive(self, conversation: Conversation) -> list[Message]:
        blocks = self._turn_blocks(conversation.messages)
        if len(blocks) <= self.policy.keep_last_turns:
            return []
        archive_blocks = blocks[: -self.policy.keep_last_turns]
        result: list[Message] = []
        for block in archive_blocks:
            for msg in block:
                if msg.role == "system" or msg.condense_parent or getattr(msg, "truncation_parent", None):
                    continue
                result.append(msg)
        return result

    def _turn_blocks(self, messages: list[Message]) -> list[list[Message]]:
        blocks: list[list[Message]] = []
        current: list[Message] = []
        for msg in messages:
            if msg.role == "system" or msg.condense_parent or getattr(msg, "truncation_parent", None):
                continue
            if msg.role == "user" and not is_control_message(Message(role="user", content=extract_user_request(msg.content or ""))):
                if current:
                    blocks.append(current)
                current = [msg]
            elif current:
                current.append(msg)
            else:
                current = [msg]
        if current:
            blocks.append(current)
        return blocks

    def _clean_transient_memory(self, state: SessionState) -> int:
        updates = 0
        memory = state.memory if isinstance(state.memory, dict) else {}
        for key in list(memory):
            text_key = str(key)
            if (
                any(text_key.startswith(prefix) for prefix in self.TRANSIENT_MEMORY_PREFIXES)
                or text_key in self.TRANSIENT_MEMORY_KEYS
            ):
                memory.pop(key, None)
                updates += 1
        if updates:
            state.memory = memory
        return updates

    @staticmethod
    def _sanitize_state_summary(text: str) -> str:
        clean = str(text or "").strip()
        if not clean:
            return ""
        clean = re.sub(r"\s+", " ", clean)
        clean = re.sub(r";\s*top_titles=[^;]+(?=;\s*|\Z)", "", clean, flags=re.I)
        clean = re.sub(r";\s*stdout=[^;]+(?=;\s*|\Z)", "", clean, flags=re.I)
        clean = re.sub(r"\bJump to content\b.*?(?=;|$)", "", clean, flags=re.I)
        clean = re.sub(r"\bWhy are the US and Israel attacking Iran\?[^;|]*", "", clean, flags=re.I)
        clean = re.sub(r"\bIran declares historic victory[^;|]*", "", clean, flags=re.I)
        clean = re.sub(r"\s*\|\s*(?=;|$)", "", clean)
        clean = re.sub(r"\s{2,}", " ", clean).strip(" ;|")
        return clean[:12_000]

    @staticmethod
    def _latest_seq(conversation: Conversation) -> int:
        return max([int(getattr(msg, "seq_id", 0) or 0) for msg in conversation.messages] or [0])

    @staticmethod
    def archive_index_digest(state: SessionState) -> str:
        payload = [
            {
                "id": getattr(record, "id", ""),
                "kind": getattr(record, "kind", ""),
                "digest": getattr(record, "digest", ""),
                "updated": getattr(record, "updated_seq", 0),
            }
            for record in (state.archive_index or {}).values()
        ]
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)[:800]
