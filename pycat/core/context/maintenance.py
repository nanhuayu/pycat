"""Recoverable, batch-oriented conversation context maintenance."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any

from pycat.core.content.archive_store import SessionArchiveStore, estimate_tokens
from pycat.core.context.compression import (
    HISTORY_FALLBACK_PROJECTION_CHARS,
    MIN_LLM_COMPRESSION_CHARS,
    CompressionResult,
    CompressionSource,
    build_history_source,
    build_history_source_from_envelopes,
    combine_history_sources,
)
from pycat.core.context.history import (
    HISTORY_SUMMARY_CONTRACT,
    build_turn_blocks,
    count_user_turn_blocks,
    is_real_user_message,
    project_history,
    read_turn_prefix,
    reusable_history_summary,
    turn_fingerprint,
)
from pycat.core.llm.token_budget import estimate_conversation_tokens
from pycat.core.state.operations import archive_trace_through, remember_archive
from pycat.core.state.work_trace import compact_work_route, work_trace_refs
from pycat.models.contracts.content import ArchivedContentRecord, FileChange
from pycat.models.contracts.session_state import SessionState
from pycat.models.conversation import Conversation, Message, normalize_tool_result, tool_call_name
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
        root = self._conversation_root(conversation)
        latest_user_id = self._latest_turn_user_id(conversation)

        for block_index, block in enumerate(closed):
            if self._conversation_root(conversation) != root:
                failed += 1
                break
            if not block or not is_real_user_message(block[0]):
                continue
            if not terminal_status and self._has_live_tool_tail(block, latest_user_id):
                continue
            self._backfill_history_images(block, store=store, state=state)
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
                if enrich and str(getattr(expected_status, "value", expected_status)) != "cancelled":
                    try:
                        updated, _calls, _error = await self._enrich_turn_capsule(
                            conversation, block, existing, store=store, client=client, provider=provider,
                        )
                        enriched += int(updated)
                    except Exception:
                        failed += 1
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

                normalized_status = str(
                    getattr(expected_status, "value", expected_status) or ""
                ).strip().lower()
                if (
                    not enrich
                    or normalized_status == "cancelled"
                    or client is None
                    or provider is None
                ):
                    continue
                updated, _calls, _error = await self._enrich_turn_capsule(
                    conversation, block, record, store=store, client=client, provider=provider,
                )
                enriched += int(updated)
            except Exception:
                failed += 1

        return TurnCapsuleReport(
            created=created,
            reused=reused,
            enriched=enriched,
            failed=failed,
            content_ids=tuple(content_ids),
        )

    async def _enrich_turn_capsule(
        self, conversation: Conversation, block: list[Message], record: ArchivedContentRecord,
        *, store: SessionArchiveStore, client: Any, provider: Provider | None,
    ) -> tuple[bool, int, str]:
        if reusable_history_summary(record) or client is None or provider is None:
            return False, 0, ""
        fingerprint = turn_fingerprint(block)
        root = self._conversation_root(conversation)
        source = self._turn_summary_source(conversation, block, store)
        source = replace(source, images=tuple(dict.fromkeys([*source.images, *store.read_images(record)])))
        result, _calls, _reason = await self._summarize_source(
            source, conversation=conversation, client=client, provider=provider, content_id=record.id,
        )
        if not result.summary or result.status not in {"complete", "degraded"}:
            return False, _calls, str(result.error or _reason or "summary_unavailable")
        # The exact receipt stays in the archive. Repeating its entire result
        # and tool list here would defeat the semantic summary's token budget.
        summary = self._with_recovery_footer(
            f"{result.summary}\n\nTurn status={record.metadata.get('terminal_status', 'unknown')}"
            f"; reason={record.metadata.get('terminal_reason', '')}",
            history_content_id=record.id,
            references=self._ordered_projection_references(block, list(source.references)),
        )
        latest = self._publish_target(store, record.id)
        if (
            latest is None or latest.metadata.get("fingerprint") != fingerprint
            or not self._source_unchanged(conversation, block, fingerprint, root)
            or estimate_tokens(summary) >= source.token_estimate
        ):
            return False, _calls, "summary_rejected"
        store.write_summary_view(
            latest, summary=summary, source=f"capability__{result.capability_id or 'compress'}", model=result.model,
            metadata={"fallback": False, "summary_contract": HISTORY_SUMMARY_CONTRACT,
                      "compression_calls": int(result.calls or _calls)},
        )
        return True, _calls, ""

    @staticmethod
    def _conversation_root(conversation: Conversation) -> tuple[str, str, str | None]:
        return (conversation.id, conversation.work_dir, getattr(conversation, "data_dir", None))

    @staticmethod
    def _publish_target(store: SessionArchiveStore, content_id: str) -> ArchivedContentRecord | None:
        record = store.read_record(content_id, kind="history")
        try:
            if record is not None and store.original_matches(record, store.read_original(record)):
                return record
        except (OSError, ValueError):
            pass
        return None

    @classmethod
    def _source_unchanged(
        cls, conversation: Conversation, messages: list[Message], fingerprint: str,
        root: tuple[str, str, str | None],
    ) -> bool:
        start, end = messages[0].seq_id, messages[-1].seq_id
        current = [message for message in conversation.messages if message.role != "system" and start <= message.seq_id <= end]
        return cls._conversation_root(conversation) == root and turn_fingerprint(current) == fingerprint

    @staticmethod
    def _summary_source(record: ArchivedContentRecord, label: str) -> CompressionSource:
        return CompressionSource(
            text=f"## {label} seq={record.metadata['start_seq']}-{record.metadata['end_seq']}\n{record.summary}",
            references=tuple(dict.fromkeys([record.id, *record.references])),
        )

    def _turn_summary_source(
        self, conversation: Conversation, block: list[Message], store: SessionArchiveStore,
    ) -> CompressionSource:
        previous = read_turn_prefix(store, block)
        if previous is None:
            return build_history_source(block, None, conversation=conversation)
        tail = [message for message in block if message.seq_id > int(previous.metadata["end_seq"])]
        return combine_history_sources([
            self._summary_source(previous, "Earlier completed steps in this user turn"),
            build_history_source(tail, None, conversation=conversation),
        ])

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
        archived, summary_updated, compact_metrics = 0, False, {"compression_calls": 0}
        if candidates:
            archived, summary_updated, compact_metrics = await self._compact_messages(
                conversation, state, candidates, client=client, provider=provider,
            )
            if compact_metrics.get("fallback_reason") == "source_changed":
                return MaintenanceReport(reason="source_changed", metrics=compact_metrics)
        capsule_archived = self._select_turn_capsules(
            conversation,
            recent_turn_target=self.policy.recent_turn_target if recent_turn_target is None else recent_turn_target,
            protect_current_turn=protect_current_turn,
        )
        prefix_archived = 0
        if protect_current_turn and not archived and not capsule_archived:
            prefix_archived, prefix_metrics = await self._compact_current_turn(
                conversation, client=client, provider=provider,
                excluded=set(exclude_message_ids or set()),
            )
            if prefix_metrics:
                compact_metrics = {
                    **compact_metrics, **prefix_metrics,
                    "compression_calls": int(compact_metrics.get("compression_calls", 0))
                    + int(prefix_metrics.get("compression_calls", 0)),
                }
                if prefix_metrics.get("fallback_reason") == "source_changed":
                    return MaintenanceReport(reason="source_changed", metrics=compact_metrics)
        state.last_maintenance_seq = max(last_seq, latest_seq, int(current_seq or 0))
        changed = archived + capsule_archived + prefix_archived
        if changed or summary_updated:
            state.state_version += 1
            state.last_updated_seq = max(state.last_updated_seq, state.last_maintenance_seq)
        conversation.set_state(state)
        if prefix_archived:
            reason = "turn_prefix"
        elif not changed:
            # no_candidates only describes protection; an attempted summary that
            # failed must stay distinguishable so callers can surface the cause.
            reason = "compression_failed" if compact_metrics.get("fallback_reason") else "no_candidates"
        return MaintenanceReport(
            summarized_messages=int(summary_updated) + int(bool(prefix_archived)),
            archived_messages=changed,
            summary_updated=summary_updated,
            reason=reason,
            archive_updates=int(summary_updated) + int(bool(prefix_archived)),
            metrics={
                **diagnostics, **compact_metrics,
                "skip_reason": compact_metrics.get("skip_reason", "" if changed else "no_candidates"),
                "capsule_archived_messages": capsule_archived,
                "prefix_archived_messages": prefix_archived,
            },
        )

    async def _compact_current_turn(
        self, conversation: Conversation, *, client: Any, provider: Provider | None, excluded: set[str],
    ) -> tuple[int, dict[str, Any]]:
        blocks = self._turn_blocks(conversation.messages)
        if not blocks or client is None or provider is None:
            return 0, {}
        block = blocks[-1]
        # Native signed thinking can depend on the unchanged preceding prefix.
        # Keep that user turn exact until a provider-safe boundary is available.
        for message in block:
            reasoning = (message.metadata or {}).get("reasoning_state") or {}
            if not isinstance(reasoning, dict):
                continue
            if any(
                isinstance(item, dict) and item.get("type") in {"thinking", "redacted_thinking"}
                for item in reasoning.get("items", [])
            ):
                return 0, {"skip_reason": "provider_thinking_boundary"}
        steps = [index for index, message in enumerate(block) if message.role == "assistant"]
        if len(steps) <= 3:
            return 0, {}
        cutoff = steps[-3]
        for index, message in enumerate(block[:cutoff]):
            if message.id in excluded or self._has_incomplete_tool_call(message):
                cutoff = index
                break
        prefix = block[:cutoff]
        if len(prefix) < 2:
            return 0, {}
        store = SessionArchiveStore(conversation.work_dir, conversation.id, data_dir=getattr(conversation, "data_dir", None))
        previous = read_turn_prefix(store, prefix)
        if previous is not None and int(previous.metadata["end_seq"]) >= prefix[-1].seq_id:
            return 0, {"skip_reason": "prefix_up_to_date"}
        root = self._conversation_root(conversation)
        payload, references, images = self._exact_history_payload(
            prefix, state=conversation.get_state(), end_seq=prefix[-1].seq_id,
            store=store, include_work_trace=False,
        )
        fingerprint = turn_fingerprint(prefix)
        payload.update({"kind": "turn_prefix", "fingerprint": fingerprint})
        source = self._turn_summary_source(conversation, prefix, store)
        source = replace(source, images=tuple(dict.fromkeys([*source.images, *images])))
        record = store.write_original(
            kind="history", title=f"prefix_{prefix[0].seq_id}_{prefix[-1].seq_id}",
            content=json.dumps(payload, ensure_ascii=False, indent=2), source="turn_prefix",
            seq_id=prefix[-1].seq_id, extension=".json", images=images,
            metadata={
                "scope": "turn_prefix", "fingerprint": fingerprint,
                "start_seq": prefix[0].seq_id, "end_seq": prefix[-1].seq_id,
                "user_message_id": prefix[0].id, "message_ids": [message.id for message in prefix],
                "references": references, "terminal_status": "running",
            },
        )
        result, calls, reason = await self._summarize_source(
            source, conversation=conversation, client=client, provider=provider, content_id=record.id,
        )
        metrics = {"compression_calls": calls, "input_chars": source.chars, "content_id": record.id}
        if not result.summary or result.status not in {"complete", "degraded"}:
            return 0, {**metrics, "fallback_reason": reason or "summary_unavailable",
                       "compression_error": str(result.error or reason or "")}
        summary = self._with_recovery_footer(
            result.summary, history_content_id=record.id,
            references=self._ordered_projection_references(prefix, references),
        )
        # Compare against the actually selected previous W plus its new exact tail.
        previous_projection = project_history(conversation, messages=prefix)[1:]
        before = estimate_conversation_tokens(previous_projection)
        if estimate_tokens(summary) >= max(1, before):
            return 0, {**metrics, "fallback_reason": "summary_no_savings"}
        latest = self._publish_target(store, record.id)
        if latest is None or not self._source_unchanged(conversation, prefix, fingerprint, root):
            return 0, {**metrics, "fallback_reason": "source_changed"}
        try:
            store.write_summary_view(
                latest, summary=summary, source=f"capability__{result.capability_id or 'compress'}", model=result.model,
                metadata={"fallback": False, "summary_contract": HISTORY_SUMMARY_CONTRACT},
            )
        except Exception as exc:
            return 0, {**metrics, "fallback_reason": f"persist_failed:{exc}"}
        user = prefix[0]
        user.metadata = dict(user.metadata or {})
        user.metadata["turn_prefix_ref"] = {
            "content_id": record.id, "fingerprint": fingerprint,
            "start_seq": user.seq_id, "end_seq": prefix[-1].seq_id,
        }
        visible_ids = {message.id for message in previous_projection}
        archived = sum(message.id in visible_ids for message in prefix[1:])
        for message in prefix[1:]:
            message.archived_content_id = record.id
        return archived, {**metrics, "summary_tokens": estimate_tokens(summary), "replaceable_tokens": before}

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
        latest_user_id = self._latest_turn_user_id(conversation)
        archived = 0
        for block in completed:
            if len(block) <= 1 or self._has_live_tool_tail(block, latest_user_id):
                continue
            user = block[0]
            fingerprint = turn_fingerprint(block)
            record = self._valid_turn_capsule(store, user, fingerprint)
            if record is None or not record.summary:
                continue
            tail = block[1:]
            if all(str(message.archived_content_id or "") == record.id for message in tail):
                continue
            exact_tokens = estimate_conversation_tokens(project_history(conversation, messages=block)[1:])
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
        original_prefix = [message for message in conversation.messages if message.role != "system" and message.seq_id <= end_seq]
        self._backfill_history_images(original_prefix, store=store, state=state)
        fingerprint = turn_fingerprint(original_prefix)
        root = self._conversation_root(conversation)
        previous_summary = state.summary
        payload, references, history_images, source, covered_messages = await self._checkpoint_manifest(
            conversation,
            state=state,
            end_seq=end_seq,
            store=store,
            client=client,
            provider=provider,
        )
        if not self._source_unchanged(conversation, original_prefix, fingerprint, root):
            return 0, False, {"fallback_reason": "source_changed", "compression_calls": payload["capsule_summary_calls"]}
        if payload.get("capsule_enrichment_failed"):
            return 0, False, {
                "compression_calls": payload["capsule_summary_calls"],
                "fallback": True, "fallback_reason": "turn_summary_unavailable",
                "compression_error": payload.get("capsule_enrichment_error", ""),
            }
        start_seq = int(payload.get("start_seq", 0) or 0)
        record = store.write_original(
            kind="history",
            title=f"history_{start_seq}_{end_seq}",
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            source="history",
            seq_id=end_seq,
            metadata={
                "scope": "checkpoint",
                "fingerprint": fingerprint,
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
        if payload["reused_summaries"] and source.chars < MIN_LLM_COMPRESSION_CHARS and not source.images:
            # Joining already compact inputs is lossless; no extra LLM roundtrip.
            result = CompressionResult(summary=source.text, status="complete", strategy="reuse")
            calls, fallback_reason = 0, ""
        else:
            result, calls, fallback_reason = await self._summarize_source(
                source, conversation=conversation, client=client, provider=provider, content_id=record.id,
            )
        calls = int(result.calls or calls or 0) + payload["capsule_summary_calls"]
        if (
            not self._source_unchanged(conversation, original_prefix, fingerprint, root)
            or conversation.get_state().summary != previous_summary
        ):
            return 0, False, {"fallback_reason": "source_changed", "compression_calls": calls}
        if self._publish_target(store, record.id) is None:
            return 0, False, {"fallback_reason": "source_unavailable", "compression_calls": calls}
        result.calls = calls
        if not result.summary and calls:
            return 0, False, {
                "compression_calls": calls, "input_chars": source.chars,
                "fallback": True, "fallback_reason": fallback_reason or result.error,
                "compression_error": str(result.error or fallback_reason or ""),
                "content_id": record.id,
            }
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
        replaceable_tokens = estimate_tokens(str(state.summary or "")) + estimate_conversation_tokens(
            project_history(conversation, messages=candidates)
        )
        summary_tokens = estimate_tokens(result.summary)
        savings_metrics = {"replaceable_tokens": replaceable_tokens, "summary_tokens": summary_tokens}
        if summary_tokens >= max(1, replaceable_tokens):
            return 0, False, {
                **savings_metrics,
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
                    "runtime_projection"
                    if result.strategy == "reuse"
                    else f"capability__{result.capability_id or 'compress'}"
                    if result.status in {"complete", "degraded"}
                    else "runtime_deterministic"
                ),
                model=result.model,
                metadata={"fallback": result.status == "fallback", "reason": fallback_reason,
                          "summary_contract": HISTORY_SUMMARY_CONTRACT},
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
            **savings_metrics,
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
        if not summary or result.status not in {"complete", "degraded"}:
            return CompressionResult(status="fallback", error=getattr(result, "error", "summary_empty")), int(result.calls or 1), str(
                getattr(result, "error", "summary_empty") or "summary_empty"
            )
        if estimate_tokens(summary) >= max(1, source.token_estimate):
            return CompressionResult(status="fallback", error="summary_no_savings"), int(result.calls or 1), "summary_no_savings"
        return replace(result), int(result.calls or 1), ""

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
                if not message_id or message_id in seen or message.role == "system":
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
        latest_user_id = self._latest_turn_user_id(conversation)
        result: list[list[Message]] = []
        for block in prefix:
            if not block or not is_real_user_message(block[0]):
                break
            if self._has_live_tool_tail(block, latest_user_id) or any(
                str(getattr(message, "id", "") or "") in excluded for message in block
            ):
                break
            result.append(list(block))
        return result

    @staticmethod
    def _has_incomplete_tool_call(message: Message) -> bool:
        tool_calls = [tool_call for tool_call in (message.tool_calls or []) if isinstance(tool_call, dict)]
        return bool(tool_calls) and any(tool_call.get("result") is None for tool_call in tool_calls)

    @staticmethod
    def _latest_turn_user_id(conversation: Conversation) -> str:
        return next(
            (str(message.id) for message in reversed(conversation.messages) if is_real_user_message(message)),
            "",
        )

    @classmethod
    def _has_live_tool_tail(cls, block: list[Message], latest_user_id: str) -> bool:
        # Only the latest user turn can still be executing tools. A missing
        # result in an older turn is an orphan and must not pin that history.
        return (
            bool(block)
            and str(block[0].id) == latest_user_id
            and any(cls._has_incomplete_tool_call(message) for message in block)
        )

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

    async def _checkpoint_manifest(
        self,
        conversation: Conversation,
        *,
        state: SessionState,
        end_seq: int,
        store: SessionArchiveStore,
        client: Any,
        provider: Provider | None,
    ) -> tuple[dict[str, Any], list[str], list[str], CompressionSource, list[Message]]:
        covered_messages = [
            message for message in conversation.messages
            if message.role != "system" and 0 < message.seq_id <= end_seq
        ]
        if not covered_messages:
            raise ValueError("checkpoint manifest requires a completed conversation prefix")
        base, base_manifest = self._reusable_checkpoint(conversation, covered_messages, store)
        base_end = int(base.metadata["end_seq"]) if base is not None else 0
        # Keep a flat exact manifest. Reuse its references, not its old tool bodies.
        sources = list(base_manifest.get("sources") or [])
        source_capsule_ids = list(base_manifest.get("source_capsule_ids") or [])
        source_fingerprints = dict(base_manifest.get("source_fingerprints") or {})
        references = list(base_manifest.get("refs") or [])
        self._extend_unique(references, work_trace_refs(state.work_trace, through_seq=end_seq))
        history_images: list[str] = []
        parts = [self._summary_source(base, "Previous continuation summary")] if base is not None else []
        summary_calls = 0
        enrichment_failed = False
        enrichment_error = ""
        reused_summaries = int(base is not None)
        root = self._conversation_root(conversation)
        latest_user_id = self._latest_turn_user_id(conversation)

        for block in build_turn_blocks(covered_messages):
            if self._conversation_root(conversation) != root:
                enrichment_failed = True
                break
            if not block or not is_real_user_message(block[0]):
                continue
            block_end = max(message.seq_id for message in block)
            if block_end <= base_end:
                continue
            if self._has_live_tool_tail(block, latest_user_id):
                raise ValueError("checkpoint cannot cross an incomplete tool batch")
            fingerprint = turn_fingerprint(block)
            record = self._valid_turn_capsule(store, block[0], fingerprint)
            envelope = self._read_turn_capsule_envelope(store, record, block=block, fingerprint=fingerprint)
            inline_images = []
            source_item = {
                "fingerprint": fingerprint, "start_seq": block[0].seq_id,
                "end_seq": block_end, "message_ids": [message.id for message in block],
            }
            if envelope is not None and record is not None:
                sources.append({**source_item, "type": "turn_capsule", "content_id": record.id})
                source_capsule_ids.append(record.id)
                source_fingerprints[record.id] = fingerprint
                self._extend_unique(references, record.references)
                enriched, calls, error = await self._enrich_turn_capsule(
                    conversation, block, record, store=store, client=client, provider=provider,
                )
                summary_calls += calls
                if enriched:
                    record = store.read_record(record.id, kind="history")
                elif calls:
                    enrichment_failed = True
                    enrichment_error = error
                    break
                if reusable_history_summary(record):
                    parts.append(self._summary_source(record, "Completed user turn"))
                    reused_summaries += 1
                    continue
                inline_images = store.read_images(record)
            else:
                envelope, _refs, inline_images = self._exact_history_payload(
                    block, state=state, end_seq=block_end, store=store, include_work_trace=False,
                )
                self._extend_unique(history_images, inline_images)
                sources.append({**source_item, "type": "inline_turn", "envelope": envelope})
            # Missing/legacy/failed summaries never become authoritative inputs.
            if read_turn_prefix(store, block) is not None:
                parts.append(self._turn_summary_source(conversation, block, store))
            else:
                parts.append(build_history_source_from_envelopes(
                    [envelope], conversation=conversation, images=inline_images,
                ))

        source = combine_history_sources(parts)
        self._extend_unique(references, source.references)
        payload = {
            "kind": "history_checkpoint_manifest", "version": 2,
            "start_seq": covered_messages[0].seq_id, "end_seq": end_seq,
            "sources": sources, "source_capsule_ids": source_capsule_ids,
            "source_fingerprints": source_fingerprints,
            "message_ids": [message.id for message in covered_messages],
            "base_checkpoint_id": base.id if base is not None else "",
            "reused_summaries": reused_summaries, "capsule_summary_calls": summary_calls,
            "capsule_enrichment_failed": enrichment_failed,
            "capsule_enrichment_error": enrichment_error,
            "work_trace_route": compact_work_route(state.work_trace, through_seq=end_seq),
            "refs": references, "image_count": len(history_images),
        }
        return payload, references, history_images, source, covered_messages

    @staticmethod
    def _reusable_checkpoint(
        conversation: Conversation, covered_messages: list[Message], store: SessionArchiveStore,
    ) -> tuple[ArchivedContentRecord | None, dict[str, Any]]:
        content_id = str(covered_messages[0].archived_content_id or "")
        record = store.read_record(content_id, kind="history") if content_id else None
        if (
            not reusable_history_summary(record)
            or record.metadata.get("scope") != "checkpoint"
            or record.summary != conversation.get_state().summary
        ):
            return None, {}
        end_seq = int(record.metadata.get("end_seq", 0) or 0)
        prefix = [message for message in covered_messages if message.seq_id <= end_seq]
        if (
            not prefix or [message.id for message in prefix] != record.metadata.get("message_ids")
            or turn_fingerprint(prefix) != record.metadata.get("fingerprint")
            or any(message.archived_content_id != content_id for message in prefix)
        ):
            return None, {}
        try:
            payload = json.loads(store.read_original(record))
        except (OSError, ValueError):
            return None, {}
        if payload.get("kind") != "history_checkpoint_manifest" or payload.get("version") != 2:
            return None, {}
        return record, payload

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
    def _backfill_history_images(
        messages: list[Message], *, store: SessionArchiveStore, state: SessionState,
    ) -> None:
        # Pin legacy inline images before fingerprinting. Attaching an image can
        # legitimately change the tool Archive id; it is not a concurrent edit.
        for message in messages:
            if any(
                isinstance(call, dict)
                and ContextMaintenance._tool_result_images(call, normalize_tool_result(call.get("result")))
                for call in message.tool_calls or []
            ):
                ContextMaintenance._message_for_history(message, store=store, state=state, references=[])

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
    def _latest_seq(conversation: Conversation) -> int:
        return max([int(getattr(message, "seq_id", 0) or 0) for message in conversation.messages] or [0])
