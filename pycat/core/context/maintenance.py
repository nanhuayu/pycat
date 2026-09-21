"""Recoverable, batch-oriented conversation context maintenance."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from pycat.core.content.archive_store import SessionArchiveStore, estimate_tokens
from pycat.core.context.compression import (
    HISTORY_FALLBACK_PROJECTION_CHARS,
    MIN_LLM_COMPRESSION_CHARS,
    CompressionResult,
    CompressionSource,
    build_history_source,
    build_history_source_from_envelopes,
)
from pycat.core.context.history import (
    build_turn_blocks,
    count_user_turn_blocks,
    is_real_user_message,
    turn_fingerprint,
)
from pycat.core.llm.token_budget import estimate_conversation_tokens
from pycat.core.state.operations import archive_trace_through, remember_archive
from pycat.core.state.work_trace import compact_work_route, work_trace_refs
from pycat.models.contracts.content import FileChange
from pycat.models.conversation import Conversation, Message, normalize_tool_result, tool_call_name
from pycat.models.contracts.session_state import SessionState
from pycat.models.provider import Provider


@dataclass(frozen=True)
class MaintenancePolicy:
    recent_turn_target: int = 3
    token_threshold_ratio: float = 0.80


@dataclass(frozen=True)
class MaintenanceReport:
    summarized_messages: int = 0
    archived_messages: int = 0
    memory_updates: int = 0
    summary_updated: bool = False
    reason: str = ""
    snipped_messages: int = 0
    archive_updates: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TurnCapsuleReport:
    created: int = 0
    reused: int = 0
    enriched: int = 0
    failed: int = 0
    content_ids: tuple[str, ...] = ()


class ContextMaintenance:
    """Archive old transcript blocks and replace them with one continuation summary."""

    def __init__(
        self,
        policy: MaintenancePolicy | None = None,
        *,
        compression_factory: Any = None,
        debug_trace: Any = None,
    ) -> None:
        self.policy = policy or MaintenancePolicy()
        self.compression_factory = compression_factory
        self.debug_trace = debug_trace

    def _compressor(self, *, provider: Provider):
        if self.compression_factory is None:
            return None
        return self.compression_factory(
            provider=provider,
            debug_trace=self.debug_trace,
        )

    async def finalize_closed_turns_async(
        self,
        conversation: Conversation,
        *,
        client: Any = None,
        provider: Provider | None = None,
        close_current_turn: bool = False,
        terminal_status: object = "",
        terminal_reason: object = "",
        enrich: bool = True,
        capture_metadata: dict | None = None,
        on_archive: Any = None,
    ) -> TurnCapsuleReport:
        """Persist dormant, recoverable sidecars for closed real-user turns."""
        blocks = self._turn_blocks(conversation.messages)
        closed = blocks if close_current_turn else blocks[:-1]
        state = conversation.get_state()
        store = SessionArchiveStore(
            getattr(conversation, "work_dir", "") or "",
            conversation_id=getattr(conversation, "id", None),
            data_dir=getattr(conversation, "data_dir", None),
        )
        created = reused = enriched = failed = 0
        content_ids: list[str] = []

        for block_index, block in enumerate(closed):
            if not block or not is_real_user_message(block[0]):
                continue
            if not terminal_status and any(self._has_incomplete_tool_call(message) for message in block):
                continue
            fingerprint = turn_fingerprint(block)
            user = block[0]
            is_terminal_block = block_index == len(closed) - 1
            expected_status = terminal_status if is_terminal_block else ""
            expected_reason = terminal_reason if is_terminal_block else ""
            existing = self._valid_turn_capsule(
                store,
                user,
                fingerprint,
                terminal_status=expected_status,
                terminal_reason=expected_reason,
            )
            if existing is not None:
                reused += 1
                content_ids.append(existing.id)
                if on_archive is not None:
                    on_archive(existing)
                continue
            try:
                end_seq = max(int(getattr(message, "seq_id", 0) or 0) for message in block)
                payload, references, history_images = self._exact_history_payload(
                    block,
                    state=state,
                    end_seq=end_seq,
                    store=store,
                    include_work_trace=False,
                )
                receipt = self._turn_capsule_receipt(
                    block,
                    payload,
                    references,
                    terminal_status=terminal_status if is_terminal_block else "",
                    terminal_reason=terminal_reason if is_terminal_block else "",
                )
                payload.update(
                    {
                        "kind": "turn_capsule",
                        "fingerprint": fingerprint,
                        "receipt": receipt,
                    }
                )
                start_seq = int(getattr(user, "seq_id", 0) or 0)
                record = store.write_original(
                    kind="history",
                    title=f"turn_{start_seq}_{end_seq}",
                    content=json.dumps(payload, ensure_ascii=False, indent=2),
                    source="turn_capsule",
                    seq_id=end_seq,
                    metadata={
                        "scope": "turn_capsule",
                        "fingerprint": fingerprint,
                        "start_seq": start_seq,
                        "end_seq": end_seq,
                        "user_message_id": user.id,
                        "message_ids": [message.id for message in block],
                        "references": references,
                        "terminal_status": receipt["terminal"]["status"],
                        "terminal_reason": receipt["terminal"]["reason"],
                        "trust": "mixed_provenance",
                        "memory_capture": dict(capture_metadata or {}),
                    },
                    extension=".json",
                    images=history_images,
                )
                # Source registration must not depend on derived summary publication.
                if on_archive is not None:
                    on_archive(record)
                fallback = self._render_turn_capsule_receipt(receipt, record.id)
                record = store.write_summary_view(
                    record,
                    summary=fallback,
                    source="runtime_deterministic",
                    metadata={"fallback": True, "scope": "turn_capsule"},
                )
                user.metadata = dict(user.metadata or {})
                user.metadata["turn_capsule_ref"] = {
                    "content_id": record.id,
                    "fingerprint": fingerprint,
                    "start_seq": start_seq,
                    "end_seq": end_seq,
                }
                created += 1
                content_ids.append(record.id)

                source = build_history_source(block, None, conversation=conversation)
                normalized_status = str(
                    getattr(expected_status, "value", expected_status) or ""
                ).strip().lower()
                if (
                    not enrich
                    or normalized_status == "cancelled"
                    or source.chars < MIN_LLM_COMPRESSION_CHARS
                    or client is None
                    or provider is None
                ):
                    continue
                result, _calls, _reason = await self._summarize_source(
                    source,
                    conversation=conversation,
                    client=client,
                    provider=provider,
                    content_id=record.id,
                )
                narrative = str(result.summary or "").strip()
                combined = f"{narrative}\n\nStructured receipt:\n{fallback}".strip() if narrative else ""
                latest = store.read_record(record.id, kind="history")
                if (
                    latest is not None
                    and latest.metadata.get("fingerprint") == fingerprint
                    and combined
                    and estimate_tokens(combined) < max(1, source.token_estimate)
                ):
                    store.write_summary_view(
                        latest,
                        summary=combined,
                        source=f"capability__{result.capability_id or 'compress'}",
                        model=result.model,
                        metadata={"fallback": False, "scope": "turn_capsule"},
                    )
                    enriched += 1
            except Exception:
                failed += 1

        return TurnCapsuleReport(
            created=created,
            reused=reused,
            enriched=enriched,
            failed=failed,
            content_ids=tuple(content_ids),
        )

    @staticmethod
    def _valid_turn_capsule(
        store: SessionArchiveStore,
        user: Message,
        fingerprint: str,
        *,
        terminal_status: object = "",
        terminal_reason: object = "",
    ):
        ref = (user.metadata or {}).get("turn_capsule_ref") if isinstance(user.metadata, dict) else None
        if not isinstance(ref, dict) or str(ref.get("fingerprint") or "") != fingerprint:
            return None
        record = store.read_record(str(ref.get("content_id") or ""), kind="history")
        metadata = record.metadata if record is not None and isinstance(record.metadata, dict) else {}
        if metadata.get("scope") != "turn_capsule" or metadata.get("fingerprint") != fingerprint:
            return None
        expected_status = str(getattr(terminal_status, "value", terminal_status) or "").strip().lower()
        expected_reason = str(getattr(terminal_reason, "value", terminal_reason) or "").strip()
        if expected_status == "completed" and expected_reason == "completed":
            expected_reason = ""
        if expected_status and str(metadata.get("terminal_status") or "") != expected_status:
            return None
        if expected_status and str(metadata.get("terminal_reason") or "") != expected_reason:
            return None
        return record

    @staticmethod
    def _turn_capsule_receipt(
        block: list[Message],
        payload: dict[str, Any],
        references: list[str],
        *,
        terminal_status: object = "",
        terminal_reason: object = "",
    ) -> dict[str, Any]:
        user = block[0]
        assistants = [message for message in block[1:] if message.role == "assistant"]
        terminal = assistants[-1] if assistants else None
        terminal_metadata = (
            terminal.metadata
            if terminal is not None and isinstance(terminal.metadata, dict)
            else {}
        )
        inferred_reason = str(
            terminal_metadata.get("interrupt_reason")
            or terminal_metadata.get("incomplete_reason")
            or ("runtime_error" if terminal_metadata.get("runtime_error") else "")
            or ""
        ).strip()
        inferred_status = (
            "no_assistant_result"
            if terminal is None
            else "failed"
            if terminal_metadata.get("runtime_error")
            else "interrupted"
            if terminal_metadata.get("interrupted") or terminal_metadata.get("incomplete")
            else "completed"
        )
        explicit_status = str(getattr(terminal_status, "value", terminal_status) or "").strip().lower()
        if explicit_status not in {"completed", "interrupted", "cancelled", "failed"}:
            explicit_status = ""
        receipt_status = explicit_status or inferred_status
        explicit_reason = str(getattr(terminal_reason, "value", terminal_reason) or "").strip()
        if explicit_status == "completed" and explicit_reason == "completed":
            explicit_reason = ""
        receipt_reason = explicit_reason if explicit_status else inferred_reason
        tools: list[dict[str, Any]] = []
        for message in payload.get("messages", []):
            if not isinstance(message, dict):
                continue
            for tool in message.get("tool_calls", []):
                if isinstance(tool, dict):
                    tools.append(dict(tool))
        visible_result = str(getattr(terminal, "content", "") or "").strip()
        return {
            "coverage": {
                "start_seq": int(getattr(user, "seq_id", 0) or 0),
                "end_seq": max(int(getattr(message, "seq_id", 0) or 0) for message in block),
                "message_ids": [message.id for message in block],
            },
            "user_anchor": {
                "id": user.id,
                "seq_id": int(getattr(user, "seq_id", 0) or 0),
                "content_refs": [ref.to_dict() for ref in (user.content_refs or [])],
            },
            "terminal": {
                "status": receipt_status,
                "reason": receipt_reason,
                "assistant_message_id": str(getattr(terminal, "id", "") or ""),
                "visible_result": visible_result[:4_000],
                "no_result_reason": "" if terminal is not None else "assistant_result_missing",
            },
            "tools": tools,
            "references": list(references),
            "provenance": {
                "user": "trusted_user_input",
                "tool_observations": "untrusted_data",
                "receipt": "system_record",
            },
        }

    @staticmethod
    def _render_turn_capsule_receipt(receipt: dict[str, Any], content_id: str) -> str:
        coverage = receipt.get("coverage") if isinstance(receipt.get("coverage"), dict) else {}
        terminal = receipt.get("terminal") if isinstance(receipt.get("terminal"), dict) else {}
        lines = [
            "Turn receipt",
            f"coverage={coverage.get('start_seq', 0)}-{coverage.get('end_seq', 0)}",
            f"status={terminal.get('status', 'unknown')}",
        ]
        if str(terminal.get("visible_result") or "").strip():
            lines.append(f"result={str(terminal.get('visible_result') or '').strip()}")
        if str(terminal.get("no_result_reason") or "").strip():
            lines.append(f"no_result_reason={terminal.get('no_result_reason')}")
        if str(terminal.get("reason") or "").strip():
            lines.append(f"terminal_reason={terminal.get('reason')}")
        for tool in receipt.get("tools", []):
            if not isinstance(tool, dict):
                continue
            line = f"tool={tool.get('name', 'tool')} call_id={tool.get('id', '')}"
            if tool.get("content_id"):
                line += f" content_id={tool.get('content_id')}"
            if tool.get("is_error"):
                line += " status=error"
            if isinstance(tool.get("file_change"), dict) and tool["file_change"].get("path"):
                line += f" file={tool['file_change'].get('path')}"
            lines.append(line)
        for reference in receipt.get("references", []):
            lines.append(f"ref={json.dumps(str(reference), ensure_ascii=False)}")
        lines.append("tool_observation_trust=untrusted_data")
        lines.append(f"exact_history_content_id={content_id}")
        return "\n".join(lines)

    async def maintain_async(
        self,
        conversation: Conversation,
        *,
        client,
        provider: Provider | None,
        prompt_limit: int = 0,
        current_seq: int = 0,
        force: bool = False,
        force_check: bool = False,
        request_token_estimate: int = 0,
        conversation_token_estimate: int = 0,
        exclude_message_ids: set[str] | None = None,
        recent_turn_target: int | None = None,
        protect_current_turn: bool = False,
    ) -> MaintenanceReport:
        state = conversation.get_state()
        last_seq = int(getattr(state, "last_maintenance_seq", 0) or 0)
        latest_seq = self._latest_seq(conversation)
        if not force and not force_check and latest_seq <= last_seq:
            return MaintenanceReport(reason="up_to_date", metrics={"skip_reason": "up_to_date"})

        diagnostics = self._diagnostics(
            conversation,
            prompt_limit,
            request_token_estimate=request_token_estimate,
            conversation_token_estimate=conversation_token_estimate,
        )
        should_compact, reason = self._should_compact(
            prompt_limit,
            force=force,
            diagnostics=diagnostics,
        )
        if not should_compact:
            state.last_maintenance_seq = max(last_seq, latest_seq, int(current_seq or 0))
            conversation.set_state(state)
            metrics = {**diagnostics, "skip_reason": reason, "compression_calls": 0}
            return MaintenanceReport(reason=reason, metrics=metrics)

        candidates = self._messages_to_archive(
            conversation,
            exclude_message_ids=set(exclude_message_ids or set()),
            recent_turn_target=(
                self.policy.recent_turn_target
                if recent_turn_target is None
                else recent_turn_target
            ),
            protect_current_turn=protect_current_turn,
        )
        if not candidates:
            capsule_archived = self._select_turn_capsules(
                conversation,
                recent_turn_target=(
                    self.policy.recent_turn_target
                    if recent_turn_target is None
                    else recent_turn_target
                ),
                protect_current_turn=protect_current_turn,
            )
            state.last_maintenance_seq = max(last_seq, latest_seq, int(current_seq or 0))
            if capsule_archived:
                state.state_version += 1
                state.last_updated_seq = max(state.last_updated_seq, state.last_maintenance_seq)
            conversation.set_state(state)
            metrics = {
                **diagnostics,
                "skip_reason": "capsules" if capsule_archived else "no_candidates",
                "compression_calls": 0,
                "capsule_archived_messages": capsule_archived,
            }
            return MaintenanceReport(
                archived_messages=capsule_archived,
                reason="capsules" if capsule_archived else "no_candidates",
                metrics=metrics,
            )

        archived, summary_updated, compact_metrics = await self._compact_messages(
            conversation,
            state,
            candidates,
            client=client,
            provider=provider,
        )
        capsule_archived = self._select_turn_capsules(
            conversation,
            recent_turn_target=(
                self.policy.recent_turn_target
                if recent_turn_target is None
                else recent_turn_target
            ),
            protect_current_turn=protect_current_turn,
        )
        state.last_maintenance_seq = max(last_seq, latest_seq, int(current_seq or 0))
        if archived or capsule_archived or summary_updated:
            state.state_version += 1
            state.last_updated_seq = max(state.last_updated_seq, state.last_maintenance_seq)
        conversation.set_state(state)
        return MaintenanceReport(
            summarized_messages=1 if summary_updated else 0,
            archived_messages=archived + capsule_archived,
            summary_updated=summary_updated,
            reason=reason,
            archive_updates=1 if summary_updated else 0,
            metrics={
                **diagnostics,
                **compact_metrics,
                "capsule_archived_messages": capsule_archived,
            },
        )

    def _select_turn_capsules(
        self,
        conversation: Conversation,
        *,
        recent_turn_target: int,
        protect_current_turn: bool,
    ) -> int:
        blocks = self._turn_blocks(conversation.messages)
        completed = blocks[:-1] if protect_current_turn and blocks else blocks
        if recent_turn_target > 0:
            keep = max(0, int(recent_turn_target) - (1 if protect_current_turn else 0))
            completed = completed[-keep:] if keep else []
        store = SessionArchiveStore(conversation.work_dir, conversation.id, data_dir=getattr(conversation, "data_dir", None))
        archived = 0
        for block in completed:
            if len(block) <= 1 or any(self._has_incomplete_tool_call(message) for message in block):
                continue
            user = block[0]
            fingerprint = turn_fingerprint(block)
            record = self._valid_turn_capsule(store, user, fingerprint)
            if record is None or not record.summary:
                continue
            tail = block[1:]
            if all(str(message.archived_content_id or "") == record.id for message in tail):
                continue
            exact_tokens = estimate_conversation_tokens(tail)
            capsule_tokens = estimate_tokens(record.summary)
            if capsule_tokens >= max(1, exact_tokens):
                continue
            for message in tail:
                message.archived_content_id = record.id
                archived += 1
        return archived

    def _should_compact(
        self,
        prompt_limit: int,
        *,
        force: bool,
        diagnostics: dict[str, int],
    ) -> tuple[bool, str]:
        if force:
            return True, "force"
        if prompt_limit <= 0:
            return False, "below_threshold"
        threshold = int(prompt_limit * self.policy.token_threshold_ratio)
        estimate = max(
            int(diagnostics.get("request_tokens", 0) or 0),
            int(diagnostics.get("conversation_tokens", 0) or 0),
            int(diagnostics.get("active_tokens", 0) or 0),
        )
        return (True, "token_pressure") if estimate >= threshold else (False, "below_threshold")

    async def _compact_messages(
        self,
        conversation: Conversation,
        state: SessionState,
        candidates: list[Message],
        *,
        client: Any,
        provider: Provider | None,
    ) -> tuple[int, bool, dict[str, Any]]:
        store = SessionArchiveStore(
            getattr(conversation, "work_dir", "") or "",
            conversation_id=getattr(conversation, "id", None),
            data_dir=getattr(conversation, "data_dir", None),
        )
        end_seq = max(int(getattr(message, "seq_id", 0) or 0) for message in candidates)
        payload, references, history_images, source, covered_messages = self._checkpoint_manifest(
            conversation,
            state=state,
            end_seq=end_seq,
            store=store,
        )
        start_seq = int(payload.get("start_seq", 0) or 0)
        record = store.write_original(
            kind="history",
            title=f"history_{start_seq}_{end_seq}",
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            source="history",
            seq_id=end_seq,
            metadata={
                "scope": "checkpoint",
                "start_seq": start_seq,
                "end_seq": end_seq,
                "message_ids": [message.id for message in covered_messages],
                "source_capsule_ids": list(payload.get("source_capsule_ids") or []),
                "source_fingerprints": dict(payload.get("source_fingerprints") or {}),
                "inline_source_count": sum(
                    1
                    for item in (payload.get("sources") or [])
                    if isinstance(item, dict) and item.get("type") == "inline_turn"
                ),
                "route": payload.get("work_trace_route", ""),
                "references": references,
            },
            extension=".json",
            images=history_images,
        )
        result, calls, fallback_reason = await self._summarize_source(
            source,
            conversation=conversation,
            client=client,
            provider=provider,
            content_id=record.id,
        )
        if result.summary:
            result.summary = self._with_recovery_footer(
                result.summary,
                history_content_id=record.id,
                references=self._ordered_projection_references(candidates, references),
            )
            result.token_estimate = estimate_tokens(result.summary)
        if not result.summary:
            result = CompressionResult(
                summary=self._deterministic_summary(
                    covered_messages,
                    record.id,
                    references=references,
                ),
                status="fallback",
                error=fallback_reason or result.error or "summary_unavailable",
            )
            fallback_reason = result.error
        replaceable_tokens = estimate_tokens(str(state.summary or "")) + estimate_conversation_tokens(candidates)
        if estimate_tokens(result.summary) >= max(1, replaceable_tokens):
            return 0, False, {
                "input_chars": source.chars,
                "output_chars": len(result.summary),
                "compression_calls": int(result.calls or calls or 0),
                "fallback": True,
                "fallback_reason": "summary_no_savings",
                "content_id": record.id,
            }

        record.metadata = dict(record.metadata or {})
        record.metadata.update(
            {
                "compression_input_chars": source.chars,
                "compression_output_chars": len(result.summary),
                "compression_calls": int(result.calls or calls or 0),
                "compression_chunks": int(result.chunks or 0),
                "compression_reduce_levels": int(result.reduce_levels or 0),
                "compression_strategy": str(result.strategy or ""),
                "compression_vision_fallback": bool(result.vision_fallback),
                "compression_fallback": result.status == "fallback",
                "compression_fallback_reason": fallback_reason,
            }
        )
        try:
            updated = store.write_summary_view(
                record,
                summary=result.summary,
                source=(
                    f"capability__{result.capability_id or 'compress'}"
                    if result.status == "complete"
                    else "runtime_deterministic"
                ),
                model=result.model,
                metadata={"fallback": result.status == "fallback", "reason": fallback_reason},
            )
        except Exception as exc:
            return 0, False, {
                "input_chars": source.chars,
                "output_chars": 0,
                "compression_calls": int(result.calls or calls or 0),
                "fallback": True,
                "fallback_reason": f"persist_failed:{exc}",
                "content_id": record.id,
            }
        if not updated.summary:
            return 0, False, {
                "input_chars": source.chars,
                "output_chars": 0,
                "compression_calls": int(result.calls or calls or 0),
                "fallback": True,
                "fallback_reason": "persisted_summary_empty",
                "content_id": record.id,
            }

        state.summary = updated.summary
        for message in covered_messages:
            message.archived_content_id = updated.id
        try:
            archive_trace_through(state.work_trace, end_seq)
        except Exception:
            pass
        return len(candidates), True, {
            "input_chars": source.chars,
            "output_chars": len(updated.summary),
            "compression_calls": int(result.calls or calls or 0),
            "skip_reason": "below_min_chars" if source.chars < MIN_LLM_COMPRESSION_CHARS and not source.images else "",
            "fallback": result.status == "fallback",
            "fallback_reason": fallback_reason,
            "content_id": updated.id,
            "tool_inputs": source.tool_count,
            "compression_chunks": int(result.chunks or 0),
            "compression_reduce_levels": int(result.reduce_levels or 0),
            "compression_strategy": str(result.strategy or ""),
            "compression_vision_fallback": bool(result.vision_fallback),
        }

    async def _summarize_source(
        self,
        source: CompressionSource,
        *,
        conversation: Conversation,
        client: Any,
        provider: Provider | None,
        content_id: str,
    ) -> tuple[CompressionResult, int, str]:
        if source.chars < MIN_LLM_COMPRESSION_CHARS and not source.images:
            return CompressionResult(status="fallback"), 0, "below_min_chars"
        if client is None or provider is None:
            return CompressionResult(status="fallback"), 0, "llm_unavailable"
        compressor = self._compressor(provider=provider)
        if compressor is None:
            return CompressionResult(status="fallback"), 0, "compressor_unavailable"
        try:
            result = await compressor.compress(
                source.text,
                purpose="history",
                images=list(source.images),
                conversation=conversation,
                trace_purpose="history_compaction",
                content_id=content_id,
            )
        except Exception as exc:
            return CompressionResult(status="fallback", error=str(exc)), 1, "compressor_error"
        summary = str(getattr(result, "summary", "") or "").strip()
        if not summary:
            return CompressionResult(status="fallback", error=getattr(result, "error", "summary_empty")), 1, str(
                getattr(result, "error", "summary_empty") or "summary_empty"
            )
        if estimate_tokens(summary) >= max(1, source.token_estimate):
            return CompressionResult(status="fallback", error="summary_no_savings"), 1, "summary_no_savings"
        return result, int(result.calls or 1), ""

    def _messages_to_archive(
        self,
        conversation: Conversation,
        *,
        exclude_message_ids: set[str] | None = None,
        recent_turn_target: int = 3,
        protect_current_turn: bool = False,
    ) -> list[Message]:
        messages: list[Message] = []
        seen: set[str] = set()
        for block in self._blocks_to_archive(
            conversation,
            exclude_message_ids=set(exclude_message_ids or set()),
            recent_turn_target=recent_turn_target,
            protect_current_turn=protect_current_turn,
        ):
            for message in block:
                message_id = str(getattr(message, "id", "") or "")
                if (
                    not message_id
                    or message_id in seen
                    or message.role == "system"
                    or self._has_incomplete_tool_call(message)
                ):
                    continue
                seen.add(message_id)
                messages.append(message)
        order = {str(message.id): index for index, message in enumerate(conversation.messages)}
        messages.sort(key=lambda message: order.get(str(message.id), len(order)))
        return messages

    def _blocks_to_archive(
        self,
        conversation: Conversation,
        *,
        exclude_message_ids: set[str] | None = None,
        recent_turn_target: int = 3,
        protect_current_turn: bool = False,
    ) -> list[list[Message]]:
        excluded = set(exclude_message_ids or set())
        blocks = self._turn_blocks(conversation.messages)
        keep = max(1 if protect_current_turn else 0, int(recent_turn_target or 0))
        prefix = blocks[:-keep] if keep else blocks
        result: list[list[Message]] = []
        for block in prefix:
            if not block or not is_real_user_message(block[0]):
                break
            if any(
                str(getattr(message, "id", "") or "") in excluded
                or self._has_incomplete_tool_call(message)
                for message in block
            ):
                break
            result.append(list(block))
        return result

    @staticmethod
    def _filter_block(block: list[Message], excluded: set[str]) -> list[Message]:
        return [
            message
            for message in block
            if message.role != "system"
            and str(getattr(message, "id", "") or "") not in excluded
            and not message.archived_content_id
        ]

    @staticmethod
    def _has_incomplete_tool_call(message: Message) -> bool:
        tool_calls = [tool_call for tool_call in (message.tool_calls or []) if isinstance(tool_call, dict)]
        return bool(tool_calls) and any(tool_call.get("result") is None for tool_call in tool_calls)

    def _exact_history_payload(
        self,
        messages: list[Message],
        *,
        state: SessionState,
        end_seq: int,
        store: SessionArchiveStore,
        include_work_trace: bool = True,
    ) -> tuple[dict[str, Any], list[str], list[str]]:
        references = (
            list(work_trace_refs(state.work_trace, through_seq=end_seq))
            if include_work_trace
            else []
        )
        history_images: list[str] = []
        message_payloads: list[dict[str, Any]] = []
        for message in messages:
            item, images = self._message_for_history(
                message,
                store=store,
                state=state,
                references=references,
            )
            message_payloads.append(item)
            for image in images:
                if image not in history_images:
                    history_images.append(image)
        payload = {
            "kind": "history_batch",
            "start_seq": min(int(getattr(message, "seq_id", 0) or 0) for message in messages),
            "end_seq": end_seq,
            "messages": message_payloads,
            "work_trace_route": (
                compact_work_route(state.work_trace, through_seq=end_seq)
                if include_work_trace
                else ""
            ),
            "refs": references,
            "image_count": len(history_images),
        }
        return payload, references, history_images

    def _checkpoint_manifest(
        self,
        conversation: Conversation,
        *,
        state: SessionState,
        end_seq: int,
        store: SessionArchiveStore,
    ) -> tuple[dict[str, Any], list[str], list[str], CompressionSource, list[Message]]:
        sources: list[dict[str, Any]] = []
        envelopes: list[dict[str, Any]] = []
        references = list(work_trace_refs(state.work_trace, through_seq=end_seq))
        history_images: list[str] = []
        covered_messages: list[Message] = []
        source_capsule_ids: list[str] = []
        source_fingerprints: dict[str, str] = {}

        blocks = build_turn_blocks(
            [
                message
                for message in conversation.messages
                if message.role != "system"
                and 0 < int(getattr(message, "seq_id", 0) or 0) <= end_seq
            ]
        )
        for block in blocks:
            if not block or not is_real_user_message(block[0]):
                continue
            block_end = max(int(getattr(message, "seq_id", 0) or 0) for message in block)
            if block_end > end_seq or any(self._has_incomplete_tool_call(message) for message in block):
                break
            fingerprint = turn_fingerprint(block)
            user = block[0]
            record = self._valid_turn_capsule(store, user, fingerprint)
            envelope = self._read_turn_capsule_envelope(
                store,
                record,
                block=block,
                fingerprint=fingerprint,
            )
            message_ids = [message.id for message in block]
            if envelope is not None and record is not None:
                sources.append(
                    {
                        "type": "turn_capsule",
                        "content_id": record.id,
                        "fingerprint": fingerprint,
                        "start_seq": int(getattr(user, "seq_id", 0) or 0),
                        "end_seq": block_end,
                        "message_ids": message_ids,
                    }
                )
                source_capsule_ids.append(record.id)
                source_fingerprints[record.id] = fingerprint
                self._extend_unique(references, record.references)
                self._extend_unique(references, envelope.get("refs") or [])
                self._extend_unique(history_images, store.read_images(record))
            else:
                envelope, inline_refs, inline_images = self._exact_history_payload(
                    block,
                    state=state,
                    end_seq=block_end,
                    store=store,
                    include_work_trace=False,
                )
                sources.append(
                    {
                        "type": "inline_turn",
                        "fingerprint": fingerprint,
                        "start_seq": int(getattr(user, "seq_id", 0) or 0),
                        "end_seq": block_end,
                        "message_ids": message_ids,
                        "envelope": envelope,
                    }
                )
                self._extend_unique(references, inline_refs)
                self._extend_unique(history_images, inline_images)
            envelopes.append(envelope)
            covered_messages.extend(block)

        if not covered_messages:
            raise ValueError("checkpoint manifest requires a completed conversation prefix")
        start_seq = min(int(getattr(message, "seq_id", 0) or 0) for message in covered_messages)
        payload = {
            "kind": "history_checkpoint_manifest",
            "version": 1,
            "start_seq": start_seq,
            "end_seq": end_seq,
            "sources": sources,
            "source_capsule_ids": source_capsule_ids,
            "source_fingerprints": source_fingerprints,
            "message_ids": [message.id for message in covered_messages],
            "work_trace_route": compact_work_route(state.work_trace, through_seq=end_seq),
            "refs": references,
            "image_count": len(history_images),
        }
        source = build_history_source_from_envelopes(
            envelopes,
            conversation=conversation,
            references=references,
            images=history_images,
        )
        return payload, references, history_images, source, covered_messages

    @staticmethod
    def _read_turn_capsule_envelope(
        store: SessionArchiveStore,
        record: Any,
        *,
        block: list[Message],
        fingerprint: str,
    ) -> dict[str, Any] | None:
        if record is None:
            return None
        try:
            payload = json.loads(store.read_original(record))
        except Exception:
            return None
        if not isinstance(payload, dict) or payload.get("kind") != "turn_capsule":
            return None
        if str(payload.get("fingerprint") or "") != fingerprint:
            return None
        expected_ids = [message.id for message in block]
        actual_ids = [
            str(item.get("id") or "")
            for item in (payload.get("messages") or [])
            if isinstance(item, dict)
        ]
        return payload if actual_ids == expected_ids else None

    @staticmethod
    def _extend_unique(target: list[str], values: Any) -> None:
        for value in values or []:
            clean = str(value or "").strip()
            if clean and clean not in target:
                target.append(clean)

    @staticmethod
    def _message_for_history(
        message: Message,
        *,
        store: SessionArchiveStore,
        state: SessionState,
        references: list[str],
    ) -> tuple[dict[str, Any], list[str]]:
        item: dict[str, Any] = {
            "id": message.id,
            "seq_id": int(getattr(message, "seq_id", 0) or 0),
            "role": message.role,
            "content": str(getattr(message, "content", "") or ""),
        }
        if getattr(message, "content_refs", None):
            item["content_refs"] = [ref.to_dict() for ref in message.content_refs]
            for ref in message.content_refs:
                value = str(getattr(ref, "ref", "") or "").strip()
                if value and value not in references:
                    references.append(value)
        message_images: list[str] = []
        tools: list[dict[str, Any]] = []
        for tool_call in message.tool_calls or []:
            if not isinstance(tool_call, dict):
                continue
            payload = normalize_tool_result(tool_call.get("result")) if tool_call.get("result") is not None else {}
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            content_id = str(metadata.get("content_id") or "").strip()
            result_images = ContextMaintenance._tool_result_images(tool_call, payload)
            record = store.read_record(content_id) if content_id else None
            if record is not None and result_images:
                updated = store.ensure_image_attachments(record, result_images)
                if updated is not None:
                    record = updated
                    content_id = updated.id
                    metadata = dict(metadata)
                    metadata.update(
                        {
                            "content_id": updated.id,
                            "archive_ref": updated.original_ref,
                            "archive_size": int(updated.size or 0),
                            "archive_image_count": int((updated.metadata or {}).get("image_count") or 0),
                            "archive_images_restorable": store.images_are_restorable(
                                updated,
                                expected_count=len(result_images),
                            ),
                        }
                    )
                    payload["metadata"] = metadata
                    tool_call["result"] = payload
                    tool_call["result_metadata"] = dict(metadata)
                    remember_archive(state, updated)
            record = record or (store.read_record(content_id) if content_id else None)
            archived_content_id = str(getattr(record, "id", "") or "") if record is not None else ""
            if archived_content_id:
                content_id = archived_content_id
                if content_id not in references:
                    references.append(content_id)
                for ref in getattr(record, "references", []) if record is not None else []:
                    if ref not in references:
                        references.append(ref)
            archived_images = store.read_images(record) if record is not None else []
            if record is None:
                for image in result_images:
                    if image not in message_images:
                        message_images.append(image)
            function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
            tool_item = {
                "id": str(tool_call.get("id") or ""),
                "name": tool_call_name(tool_call) or str(metadata.get("name") or "tool"),
                "arguments": function.get("arguments", ""),
                "content_id": archived_content_id,
                "result_chars": int(getattr(record, "size", 0) or 0) if record is not None else len(str(payload.get("content") or "")),
                "is_error": bool(metadata.get("is_error")),
                "error_code": str(metadata.get("error_code") or ""),
                "retryable": bool(metadata.get("retryable")),
                "references": [
                    str(ref)
                    for ref in (metadata.get("references") or [])
                    if str(ref).strip()
                ],
                "trust": "untrusted_data",
                "image_count": len(archived_images) if record is not None else len(result_images),
            }
            content_refs = [
                dict(item)
                for item in (metadata.get("content_refs") or [])
                if isinstance(item, dict)
            ]
            if content_refs:
                tool_item["content_refs"] = content_refs
                for content_ref in content_refs:
                    value = str(content_ref.get("ref") or "").strip()
                    if value and value not in references:
                        references.append(value)
            for value in tool_item["references"]:
                if value not in references:
                    references.append(value)
            if record is None:
                tool_item["result"] = payload.get("content")
            # File changes are compact provenance, not tool-result content.
            # Keep only the validated contract so history remains recoverable
            # without duplicating arbitrary result metadata.
            raw_file_change = metadata.get("file_change")
            if isinstance(raw_file_change, dict):
                try:
                    tool_item["file_change"] = FileChange.from_dict(raw_file_change).to_dict()
                except (TypeError, ValueError):
                    pass
            tools.append(tool_item)
        if tools:
            item["tool_calls"] = tools
        return item, message_images

    @staticmethod
    def _tool_result_images(tool_call: dict[str, Any], payload: dict[str, Any]) -> list[str]:
        images: list[str] = []
        for raw in (tool_call.get("result_images") or []):
            value = str(raw or "").strip()
            if value and value not in images:
                images.append(value)
        for raw in (payload.get("images") or []):
            value = str(raw or "").strip()
            if value and value not in images:
                images.append(value)
        return images

    def _deterministic_summary(
        self,
        messages: list[Message],
        history_content_id: str,
        *,
        references: list[str] | None = None,
    ) -> str:
        parts: list[str] = []
        for message in messages:
            content = re.sub(r"\s+", " ", str(getattr(message, "content", "") or "")).strip()
            if content:
                parts.append(f"{message.role}: {content[:500]}")
            for tool_call in message.tool_calls or []:
                if not isinstance(tool_call, dict):
                    continue
                payload = normalize_tool_result(tool_call.get("result")) if tool_call.get("result") is not None else {}
                metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
                name = tool_call_name(tool_call) or str(metadata.get("name") or "tool")
                content_id = str(metadata.get("content_id") or "").strip()
                summary = str(payload.get("summary") or metadata.get("tool_result_summary") or "").strip()
                detail = f"; result={summary[:260]}" if summary else ""
                ref = f"; content_id={content_id}" if content_id else ""
                parts.append(f"tool {name}{ref}{detail}")
        ordered_refs = self._ordered_projection_references(messages, references or [])
        ref_lines = [f"ref={json.dumps(value, ensure_ascii=False)}" for value in ordered_refs[:32]]
        if len(ordered_refs) > len(ref_lines):
            ref_lines.append(f"refs_omitted={len(ordered_refs) - len(ref_lines)}")
        recovery = "\n".join(
            [*ref_lines, f"Exact archived history: content_id={history_content_id}."]
        )
        body_limit = max(0, HISTORY_FALLBACK_PROJECTION_CHARS - len(recovery) - 1)
        body = "\n".join(parts).strip()[:body_limit].rstrip()
        return f"{body}\n{recovery}".strip()

    @staticmethod
    def _ordered_projection_references(
        messages: list[Message],
        references: list[str],
    ) -> list[str]:
        """Prioritize canonical content refs before broader trace/archive refs."""
        ordered: list[str] = []

        def append(value: object) -> None:
            clean = str(value or "").strip()
            if clean and clean not in ordered:
                ordered.append(clean)

        for message in messages:
            for content_ref in getattr(message, "content_refs", None) or []:
                append(getattr(content_ref, "ref", ""))
            for tool_call in getattr(message, "tool_calls", None) or []:
                if not isinstance(tool_call, dict) or tool_call.get("result") is None:
                    continue
                payload = normalize_tool_result(tool_call.get("result"))
                metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
                for content_ref in metadata.get("content_refs") or []:
                    if isinstance(content_ref, dict):
                        append(content_ref.get("ref"))
        for value in references:
            append(value)
        return ordered

    @staticmethod
    def _with_recovery_footer(
        summary: str,
        *,
        history_content_id: str,
        references: list[str],
    ) -> str:
        lines = [
            "Recovery references (system-generated):",
            *(f"ref={json.dumps(value, ensure_ascii=False)}" for value in references[:32]),
        ]
        if len(references) > 32:
            lines.append(f"refs_omitted={len(references) - 32}")
        lines.append(f"exact_history_content_id={history_content_id}")
        return f"{str(summary or '').strip()}\n\n" + "\n".join(lines)

    def _diagnostics(
        self,
        conversation: Conversation,
        prompt_limit: int,
        *,
        request_token_estimate: int = 0,
        conversation_token_estimate: int = 0,
    ) -> dict[str, int]:
        tool_stats = self._tool_loop_stats(conversation)
        return {
            "request_tokens": int(request_token_estimate or 0),
            "conversation_tokens": int(conversation_token_estimate or 0),
            "active_tokens": int(estimate_conversation_tokens(conversation) or 0),
            "threshold_tokens": int(prompt_limit * self.policy.token_threshold_ratio)
            if prompt_limit > 0
            else 0,
            "prompt_limit": int(prompt_limit or 0),
            "tool_chars": int(tool_stats.get("result_chars", 0) or 0),
            "tool_steps": int(tool_stats.get("active_messages", 0) or 0),
            "turn_blocks": count_user_turn_blocks(conversation.messages),
        }

    def _tool_loop_stats(self, conversation: Conversation) -> dict[str, int]:
        max_messages = 0
        max_chars = 0
        for block in self._turn_blocks(conversation.messages):
            active = [message for message in block if not message.archived_content_id]
            max_messages = max(max_messages, len(active))
            max_chars = max(max_chars, sum(self._message_tool_result_chars(message) for message in active))
        return {"active_messages": max_messages, "result_chars": max_chars}

    @staticmethod
    def _message_tool_result_chars(message: Message) -> int:
        total = 0
        for tool_call in message.tool_calls or []:
            if not isinstance(tool_call, dict):
                continue
            payload = normalize_tool_result(tool_call.get("result")) if tool_call.get("result") is not None else {}
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            try:
                total += int(metadata.get("tool_result_chars") or 0)
            except Exception:
                content = payload.get("content")
                total += len(content) if isinstance(content, str) else 0
        return total

    def _turn_blocks(self, messages: list[Message]) -> list[list[Message]]:
        active = [
            message
            for message in messages
            if message.role != "system"
        ]
        return [
            block
            for block in build_turn_blocks(active)
            if block
            and is_real_user_message(block[0])
            and not block[0].archived_content_id
        ]

    @staticmethod
    def _sanitize_state_summary(text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "")).strip()[:HISTORY_FALLBACK_PROJECTION_CHARS]

    @staticmethod
    def _latest_seq(conversation: Conversation) -> int:
        return max([int(getattr(message, "seq_id", 0) or 0) for message in conversation.messages] or [0])
