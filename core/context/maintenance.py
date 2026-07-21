"""Recoverable, batch-oriented conversation context maintenance."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from core.content.archive_store import SessionArchiveStore, estimate_tokens
from core.context.compression import (
    MAX_SUMMARY_CHARS,
    MIN_LLM_COMPRESSION_CHARS,
    CompressionResult,
    CompressionSource,
    build_history_source,
)
from core.context.history import count_user_turn_blocks, is_control_message
from core.context.sections import extract_user_request
from core.llm.token_budget import estimate_conversation_tokens
from core.state.operations import archive_trace_through, remember_archive
from core.state.work_trace import compact_work_route, work_trace_refs
from models.conversation import Conversation, Message, normalize_tool_result, tool_call_name
from models.contracts.session_state import SessionState
from models.provider import Provider


TOOL_LOOP_KEEP_TAIL_MESSAGES = 5
TOOL_LOOP_MIN_MESSAGES = 8
TOOL_LOOP_MAX_RESULT_CHARS = 24_000


@dataclass(frozen=True)
class MaintenancePolicy:
    keep_last_turns: int = 3
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


class ContextMaintenance:
    """Archive old transcript blocks and replace them with one continuation summary."""

    def __init__(
        self,
        policy: MaintenancePolicy | None = None,
        *,
        compressor_factory: Any = None,
        debug_trace: Any = None,
    ) -> None:
        self.policy = policy or MaintenancePolicy()
        self.compressor_factory = compressor_factory
        self.debug_trace = debug_trace

    def _compressor(self, *, client: Any, provider: Provider, store: SessionArchiveStore):
        if self.compressor_factory is None:
            return None
        return self.compressor_factory(
            client=client,
            provider=provider,
            store=store,
            debug_trace=self.debug_trace,
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
        force_check: bool = False,
        request_token_estimate: int = 0,
        conversation_token_estimate: int = 0,
        exclude_message_ids: set[str] | None = None,
    ) -> MaintenanceReport:
        state = conversation.get_state()
        last_seq = int(getattr(state, "last_maintenance_seq", 0) or 0)
        latest_seq = self._latest_seq(conversation)
        if not force and not force_check and latest_seq <= last_seq:
            return MaintenanceReport(reason="up_to_date", metrics={"skip_reason": "up_to_date"})

        diagnostics = self._diagnostics(
            conversation,
            context_window_limit,
            request_token_estimate=request_token_estimate,
            conversation_token_estimate=conversation_token_estimate,
        )
        should_compact, reason = self._should_compact(
            context_window_limit,
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
        )
        if not candidates:
            state.last_maintenance_seq = max(last_seq, latest_seq, int(current_seq or 0))
            conversation.set_state(state)
            metrics = {**diagnostics, "skip_reason": "no_candidates", "compression_calls": 0}
            return MaintenanceReport(reason="no_candidates", metrics=metrics)

        archived, summary_updated, compact_metrics = await self._compact_messages(
            conversation,
            state,
            candidates,
            client=client,
            provider=provider,
        )
        state.last_maintenance_seq = max(last_seq, latest_seq, int(current_seq or 0))
        if archived or summary_updated:
            state.state_version += 1
            state.last_updated_seq = max(state.last_updated_seq, state.last_maintenance_seq)
        conversation.set_state(state)
        return MaintenanceReport(
            summarized_messages=1 if summary_updated else 0,
            archived_messages=archived,
            summary_updated=summary_updated,
            reason=reason,
            archive_updates=1 if summary_updated else 0,
            metrics={**diagnostics, **compact_metrics},
        )

    def _should_compact(
        self,
        context_window_limit: int,
        *,
        force: bool,
        diagnostics: dict[str, int],
    ) -> tuple[bool, str]:
        if force:
            return True, "force"
        if context_window_limit <= 0:
            return False, "below_threshold"
        threshold = int(context_window_limit * self.policy.token_threshold_ratio)
        estimate = max(
            int(diagnostics.get("request_tokens", 0) or 0),
            int(diagnostics.get("conversation_tokens", 0) or 0),
            int(diagnostics.get("active_tokens", 0) or 0),
        )
        return (True, "token_pressure") if estimate > threshold else (False, "below_threshold")

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
            getattr(conversation, "work_dir", "") or ".",
            conversation_id=getattr(conversation, "id", None),
        )
        start_seq = min(int(getattr(message, "seq_id", 0) or 0) for message in candidates)
        end_seq = max(int(getattr(message, "seq_id", 0) or 0) for message in candidates)
        payload, references, history_images = self._exact_history_payload(
            candidates,
            state=state,
            end_seq=end_seq,
            store=store,
        )
        record = store.write_original(
            kind="history",
            title=f"history_{start_seq}_{end_seq}",
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            source="history",
            seq_id=end_seq,
            metadata={
                "start_seq": start_seq,
                "end_seq": end_seq,
                "message_ids": [message.id for message in candidates],
                "route": payload.get("work_trace_route", ""),
                "references": references,
            },
            extension=".json",
            images=history_images,
        )
        remember_archive(state, record)

        source = build_history_source(candidates, state, conversation=conversation)
        result, calls, fallback_reason = await self._summarize_source(
            source,
            conversation=conversation,
            store=store,
            client=client,
            provider=provider,
        )
        if not result.summary:
            result = CompressionResult(
                summary=self._deterministic_summary(candidates, state, record.id),
                status="fallback",
                error=fallback_reason or result.error or "summary_unavailable",
            )
            fallback_reason = result.error
        if estimate_tokens(result.summary) >= max(1, source.token_estimate):
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

        remember_archive(state, updated)
        state.summary = updated.summary
        for message in candidates:
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
        store: SessionArchiveStore,
        client: Any,
        provider: Provider | None,
    ) -> tuple[CompressionResult, int, str]:
        if source.chars < MIN_LLM_COMPRESSION_CHARS and not source.images:
            return CompressionResult(status="fallback"), 0, "below_min_chars"
        if client is None or provider is None:
            return CompressionResult(status="fallback"), 0, "llm_unavailable"
        compressor = self._compressor(client=client, provider=provider, store=store)
        if compressor is None:
            return CompressionResult(status="fallback"), 0, "compressor_unavailable"
        try:
            result = await compressor.compress_history(source, conversation=conversation)
        except Exception as exc:
            return CompressionResult(status="fallback", error=str(exc)), 1, "compressor_error"
        summary = str(getattr(result, "summary", "") or "").strip()
        if not summary:
            return CompressionResult(status="fallback", error=getattr(result, "error", "summary_empty")), 1, str(
                getattr(result, "error", "summary_empty") or "summary_empty"
            )
        if len(summary) > MAX_SUMMARY_CHARS:
            return CompressionResult(status="fallback", error="summary_too_long"), 1, "summary_too_long"
        if estimate_tokens(summary) >= max(1, source.token_estimate):
            return CompressionResult(status="fallback", error="summary_no_savings"), 1, "summary_no_savings"
        return result, int(result.calls or 1), ""

    def _messages_to_archive(
        self,
        conversation: Conversation,
        *,
        exclude_message_ids: set[str] | None = None,
    ) -> list[Message]:
        messages: list[Message] = []
        seen: set[str] = set()
        for block in self._blocks_to_archive(
            conversation,
            exclude_message_ids=set(exclude_message_ids or set()),
        ):
            for message in block:
                message_id = str(getattr(message, "id", "") or "")
                if (
                    not message_id
                    or message_id in seen
                    or message.role == "system"
                    or message.archived_content_id
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
    ) -> list[list[Message]]:
        excluded = set(exclude_message_ids or set())
        blocks = self._turn_blocks(conversation.messages)
        keep = max(1, int(self.policy.keep_last_turns or 3))
        old_blocks = blocks[:-keep] if len(blocks) > keep else []
        tail_blocks = blocks[-keep:]
        result = [self._filter_block(block, excluded) for block in old_blocks]
        result.extend(self._tool_loop_blocks_to_archive(tail_blocks, excluded))
        return [block for block in result if block]

    def _tool_loop_blocks_to_archive(
        self,
        blocks: list[list[Message]],
        excluded: set[str],
    ) -> list[list[Message]]:
        result: list[list[Message]] = []
        for block in blocks:
            active = self._filter_block(block, excluded)
            assistant = [message for message in active if message.role != "user"]
            if len(assistant) <= TOOL_LOOP_KEEP_TAIL_MESSAGES:
                continue
            result_chars = sum(self._message_tool_result_chars(message) for message in assistant)
            if len(assistant) < TOOL_LOOP_MIN_MESSAGES and result_chars < TOOL_LOOP_MAX_RESULT_CHARS:
                continue
            candidates = assistant[:-TOOL_LOOP_KEEP_TAIL_MESSAGES]
            if candidates:
                result.append(candidates)
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
    ) -> tuple[dict[str, Any], list[str], list[str]]:
        references = list(work_trace_refs(state.work_trace, through_seq=end_seq))
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
            "work_trace_route": compact_work_route(state.work_trace, through_seq=end_seq),
            "refs": references,
            "image_count": len(history_images),
        }
        return payload, references, history_images

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
        message_images: list[str] = []
        if str(getattr(message, "thinking", "") or ""):
            item["thinking"] = str(message.thinking or "")
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
            exact = payload.get("content")
            if content_id:
                try:
                    exact = store.read_original(content_id)
                except Exception:
                    pass
                if content_id not in references:
                    references.append(content_id)
                record = record or store.read_record(content_id)
                for ref in getattr(record, "references", []) if record is not None else []:
                    if ref not in references:
                        references.append(ref)
            archived_images = store.read_images(record) if record is not None else []
            for image in archived_images:
                if image not in message_images:
                    message_images.append(image)
            function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
            tools.append(
                {
                    "id": str(tool_call.get("id") or ""),
                    "name": tool_call_name(tool_call) or str(metadata.get("name") or "tool"),
                    "arguments": function.get("arguments", ""),
                    "content_id": content_id,
                    "result": exact,
                    "is_error": bool(metadata.get("is_error")),
                    "image_count": len(archived_images),
                }
            )
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
        state: SessionState,
        history_content_id: str,
    ) -> str:
        parts: list[str] = []
        previous = self._sanitize_state_summary(str(state.summary or ""))
        if previous:
            parts.append(f"Previous context: {previous[:1200]}")
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
        recovery = f"Exact archived history: content_id={history_content_id}."
        body_limit = max(0, MAX_SUMMARY_CHARS - len(recovery) - 1)
        body = "\n".join(parts).strip()[:body_limit].rstrip()
        return f"{body}\n{recovery}".strip()

    def _diagnostics(
        self,
        conversation: Conversation,
        context_window_limit: int,
        *,
        request_token_estimate: int = 0,
        conversation_token_estimate: int = 0,
    ) -> dict[str, int]:
        tool_stats = self._tool_loop_stats(conversation)
        return {
            "request_tokens": int(request_token_estimate or 0),
            "conversation_tokens": int(conversation_token_estimate or 0),
            "active_tokens": int(estimate_conversation_tokens(conversation) or 0),
            "threshold_tokens": int(context_window_limit * self.policy.token_threshold_ratio)
            if context_window_limit > 0
            else 0,
            "prompt_limit": int(context_window_limit or 0),
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
        blocks: list[list[Message]] = []
        current: list[Message] = []
        for message in messages:
            if message.role == "system" or message.archived_content_id:
                continue
            if message.role == "user" and not is_control_message(
                Message(role="user", content=extract_user_request(message.content or ""))
            ):
                if current:
                    blocks.append(current)
                current = [message]
            elif current:
                current.append(message)
            else:
                current = [message]
        if current:
            blocks.append(current)
        return blocks

    @staticmethod
    def _sanitize_state_summary(text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "")).strip()[:MAX_SUMMARY_CHARS]

    @staticmethod
    def _latest_seq(conversation: Conversation) -> int:
        return max([int(getattr(message, "seq_id", 0) or 0) for message in conversation.messages] or [0])
