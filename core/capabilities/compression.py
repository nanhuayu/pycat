"""Capability-backed, model-aware context compression."""
from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace
from typing import Any

from core.capabilities import CapabilityConfig
from core.content.archive_store import ArchivedContentRecord, SessionArchiveStore
from core.context.compression import (
    COMPRESSION_IMAGE_TOKEN_RESERVE,
    COMPRESSION_INPUT_SAFETY_RATIO,
    MAP_SUMMARY_CHARS,
    MAX_SUMMARY_CHARS,
    CompressionResult,
    CompressionSource,
    group_sections_by_token_budget,
    json_output_contract,
    split_text_by_token_budget,
)
from core.llm.model_selection import ResolvedModelSelection
from core.llm.token_budget import estimate_tokens, resolve_token_budget
from models.conversation import Conversation
from models.provider import Provider


logger = logging.getLogger(__name__)
MAX_REDUCE_LEVELS = 8


class CapabilityCompressionOrchestrator:
    def __init__(
        self,
        client: Any,
        provider: Provider,
        store: SessionArchiveStore | None = None,
        capability_executor: Any = None,
        debug_trace: Any = None,
    ) -> None:
        self.client = client
        self.provider = provider
        self.store = store
        self.debug_trace = debug_trace
        self.capability_executor = capability_executor

    async def summarize_archive(
        self,
        record: ArchivedContentRecord,
        *,
        conversation: Conversation | None = None,
        purpose: str = "maintenance",
    ) -> CompressionResult:
        store = self._store(conversation)
        text = store.read_original(record)
        result = await self._summarize_text(
            text,
            images=store.read_images(record),
            conversation=conversation,
            purpose=purpose,
            content_id=record.id,
            source=record.source or record.title,
            material="archived content",
        )
        return result

    async def compress_history(
        self,
        source: CompressionSource,
        *,
        conversation: Conversation | None = None,
    ) -> CompressionResult:
        return await self._summarize_text(
            source.text,
            images=list(source.images),
            conversation=conversation,
            purpose="history_compaction",
            content_id="",
            source="conversation history",
            material="conversation material",
        )

    def apply_archive_summary(
        self,
        record: ArchivedContentRecord,
        result: CompressionResult,
        *,
        conversation: Conversation | None = None,
    ) -> ArchivedContentRecord:
        store = self._store(conversation)
        record.metadata = dict(record.metadata or {})
        record.metadata.update(
            {
                "compressed_token_estimate": int(result.token_estimate or 0),
                "compression_calls": int(result.calls or 0),
                "compression_chunks": int(result.chunks or 0),
                "compression_reduce_levels": int(result.reduce_levels or 0),
                "compression_strategy": str(result.strategy or ""),
                "compression_vision_fallback": bool(result.vision_fallback),
            }
        )
        if result.summary:
            store.write_summary_view(
                record,
                summary=result.summary,
                source=f"capability__{result.capability_id or 'compress'}",
                model=result.model,
                metadata={
                    "fallback": result.status == "fallback",
                    "calls": int(result.calls or 0),
                    "chunks": int(result.chunks or 0),
                    "reduce_levels": int(result.reduce_levels or 0),
                    "strategy": str(result.strategy or ""),
                    "vision_fallback": bool(result.vision_fallback),
                },
            )
        else:
            store.mark_summary_failed(record, error=result.error or result.status)
        return store.read_record(record.id) or record

    async def _summarize_text(
        self,
        text: str,
        *,
        images: list[str],
        conversation: Conversation | None,
        purpose: str,
        content_id: str,
        source: str,
        material: str,
    ) -> CompressionResult:
        started_at = time.monotonic()
        capability = self._capability("compress")
        selection = self.capability_executor.resolve_capability_target(
            provider=self.provider,
            capability_id=capability.id,
            conversation=conversation,
        )
        usable_images = list(images or [])
        vision_fallback = False
        image_note = ""
        if usable_images and not self._supports_vision(selection):
            fallback = self._primary_vision_selection(conversation)
            if fallback is not None:
                selection = fallback
                vision_fallback = True
            else:
                image_note = (
                    f"Images: {len(usable_images)} archived image attachment(s) were not visually analyzed; "
                    "restore them with archive__read(view=\"content\", offset=0)."
                )
                usable_images = []

        prompt_budget = self._prompt_budget_tokens(
            capability,
            selection,
            image_count=len(usable_images),
        )
        full_prompt = self._document_prompt(
            text,
            purpose=purpose,
            content_id=content_id,
            source=source,
            material=material,
        )
        if estimate_tokens(full_prompt) <= prompt_budget:
            result = await self._run_capability(
                capability,
                full_prompt,
                conversation=conversation,
                selection=selection,
                images=usable_images,
                max_chars=MAX_SUMMARY_CHARS,
            )
            result.calls = 1
            result.chunks = 1
            result.strategy = "single"
            result.vision_fallback = vision_fallback
            self._append_image_note(result, image_note)
            self._record_trace(
                result,
                input_chars=len(text),
                content_id=content_id,
                purpose=purpose,
                started_at=started_at,
            )
            return result

        chunk_overhead = estimate_tokens(
            self._chunk_prompt(
                "",
                purpose=purpose,
                content_id=content_id,
                source=source,
                index=1,
                total=1,
                start=0,
                end=0,
            )
        )
        body_budget = prompt_budget - chunk_overhead
        if body_budget <= 0:
            failed = CompressionResult(
                status="error",
                capability_id=capability.id,
                error="compression_context_too_small",
                strategy="map_reduce",
                vision_fallback=vision_fallback,
            )
            self._record_trace(
                failed,
                input_chars=len(text),
                content_id=content_id,
                purpose=purpose,
                started_at=started_at,
            )
            return failed
        chunks = split_text_by_token_budget(text, body_budget)
        partials: list[str] = []
        calls = 0
        for index, chunk in enumerate(chunks, start=1):
            prompt = self._chunk_prompt(
                chunk.text,
                purpose=purpose,
                content_id=content_id,
                source=source,
                index=index,
                total=len(chunks),
                start=chunk.start,
                end=chunk.end,
            )
            result = await self._run_capability(
                capability,
                prompt,
                conversation=conversation,
                selection=selection,
                images=usable_images if index == 1 else [],
                max_chars=MAP_SUMMARY_CHARS,
            )
            calls += 1
            if not result.summary:
                result.calls = calls
                result.chunks = len(chunks)
                result.strategy = "map_reduce"
                result.vision_fallback = vision_fallback
                self._record_trace(
                    result,
                    input_chars=len(text),
                    content_id=content_id,
                    purpose=purpose,
                    started_at=started_at,
                )
                return result
            partials.append(
                f"## Chunk {index}/{len(chunks)} chars={chunk.start}-{chunk.end}\n{result.summary}"
            )

        reduce_levels = 0
        sections = partials
        while reduce_levels < MAX_REDUCE_LEVELS:
            final_prompt = self._reduce_prompt(
                sections,
                purpose=purpose,
                content_id=content_id,
                source=source,
                final=True,
            )
            if estimate_tokens(final_prompt) <= prompt_budget:
                final = await self._run_capability(
                    capability,
                    final_prompt,
                    conversation=conversation,
                    selection=selection,
                    images=[],
                    max_chars=MAX_SUMMARY_CHARS,
                )
                calls += 1
                final.calls = calls
                final.chunks = len(chunks)
                final.reduce_levels = reduce_levels + 1
                final.strategy = "map_reduce"
                final.vision_fallback = vision_fallback
                self._append_image_note(final, image_note)
                self._record_trace(
                    final,
                    input_chars=len(text),
                    content_id=content_id,
                    purpose=purpose,
                    started_at=started_at,
                )
                return final

            reduce_levels += 1
            reduce_overhead = estimate_tokens(
                self._reduce_prompt(
                    [],
                    purpose=purpose,
                    content_id=content_id,
                    source=source,
                    final=False,
                )
            )
            reduce_budget = prompt_budget - reduce_overhead
            if reduce_budget <= 0:
                break
            groups = group_sections_by_token_budget(sections, reduce_budget)
            if not groups:
                break
            reduced: list[str] = []
            for index, group in enumerate(groups, start=1):
                prompt = self._reduce_prompt(
                    group,
                    purpose=purpose,
                    content_id=content_id,
                    source=source,
                    final=False,
                )
                result = await self._run_capability(
                    capability,
                    prompt,
                    conversation=conversation,
                    selection=selection,
                    images=[],
                    max_chars=MAP_SUMMARY_CHARS,
                )
                calls += 1
                if not result.summary:
                    result.calls = calls
                    result.chunks = len(chunks)
                    result.reduce_levels = reduce_levels
                    result.strategy = "map_reduce"
                    result.vision_fallback = vision_fallback
                    self._record_trace(
                        result,
                        input_chars=len(text),
                        content_id=content_id,
                        purpose=purpose,
                        started_at=started_at,
                    )
                    return result
                reduced.append(f"## Reduced group {index}/{len(groups)}\n{result.summary}")
            sections = reduced

        failed = CompressionResult(
            status="error",
            capability_id=capability.id,
            error="compression_reduce_did_not_converge",
            calls=calls,
            chunks=len(chunks),
            reduce_levels=reduce_levels,
            strategy="map_reduce",
            vision_fallback=vision_fallback,
        )
        self._record_trace(
            failed,
            input_chars=len(text),
            content_id=content_id,
            purpose=purpose,
            started_at=started_at,
        )
        return failed

    async def _run_capability(
        self,
        capability: CapabilityConfig,
        user_prompt: str,
        *,
        conversation: Conversation | None,
        selection: ResolvedModelSelection,
        images: list[str],
        max_chars: int,
    ) -> CompressionResult:
        if self.capability_executor is None:
            return CompressionResult(status="error", capability_id=capability.id, error="capability_executor_not_injected")
        try:
            response = await self.capability_executor.run_capability(
                provider=self.provider,
                capability_id=capability.id,
                message=user_prompt,
                conversation=conversation,
                context=SimpleNamespace(
                    caller="internal",
                    source="internal",
                    entrypoint="context_maintenance",
                    debug_trace=self.debug_trace.with_purpose("condense")
                    if getattr(self.debug_trace, "enabled", False)
                    else None,
                ),
                extra_system_contract=json_output_contract(max_chars),
                title=f"runtime_{capability.id}_compression",
                images=list(images or []),
                resolved_selection=selection,
            )
        except Exception as exc:
            logger.warning("Capability compression failed via %s: %s", capability.id, exc)
            return CompressionResult(status="error", capability_id=capability.id, error=str(exc))

        result = self._parse_model_result(getattr(response, "content", ""), max_chars=max_chars)
        metadata = getattr(response, "metadata", {})
        result.model = str(
            getattr(response, "model", "")
            or (metadata.get("model") if isinstance(metadata, dict) else "")
            or selection.model
            or ""
        )
        result.capability_id = capability.id
        return result

    def _prompt_budget_tokens(
        self,
        capability: CapabilityConfig,
        selection: ResolvedModelSelection,
        *,
        image_count: int,
    ) -> int:
        provider = selection.provider or self.provider
        budget = resolve_token_budget(provider=provider, model_id=selection.model)
        effective = int(budget.effective_prompt_limit or budget.context_window or 0)
        safe = int(effective * COMPRESSION_INPUT_SAFETY_RATIO)
        overhead = estimate_tokens(str(capability.prompt or "")) + estimate_tokens(json_output_contract())
        overhead += max(0, int(image_count or 0)) * COMPRESSION_IMAGE_TOKEN_RESERVE
        return max(1, safe - overhead)

    @staticmethod
    def _supports_vision(selection: ResolvedModelSelection) -> bool:
        provider = selection.provider
        if provider is None:
            return False
        try:
            return bool(provider.effective_model_profile(selection.model).supports_vision)
        except Exception:
            return bool(getattr(provider, "supports_vision", False))

    def _primary_vision_selection(self, conversation: Conversation | None) -> ResolvedModelSelection | None:
        model = str(getattr(conversation, "model", "") or "").strip() if conversation is not None else ""
        if not model:
            model_ids = self.provider.model_ids()
            model = model_ids[0] if model_ids else ""
        selection = ResolvedModelSelection(
            provider=self.provider,
            model=model,
            source="vision_fallback",
        )
        return selection if model and self._supports_vision(selection) else None

    @staticmethod
    def _document_prompt(
        text: str,
        *,
        purpose: str,
        content_id: str,
        source: str,
        material: str,
    ) -> str:
        metadata = [f"purpose={purpose}", f"source={source}", f"chars={len(text)}"]
        if content_id:
            metadata.insert(1, f"content_id={content_id}")
        return (
            f"Condense this exact {material} into one continuation summary.\n"
            + "\n".join(metadata)
            + f"\n\n<content>\n{text}\n</content>"
        )

    @staticmethod
    def _chunk_prompt(
        text: str,
        *,
        purpose: str,
        content_id: str,
        source: str,
        index: int,
        total: int,
        start: int,
        end: int,
    ) -> str:
        content_ref = f"content_id={content_id}\n" if content_id else ""
        return (
            "Summarize this exact chunk for a later reduce step. Preserve concrete facts, references, "
            "requirements, decisions, errors and unresolved work; do not assume unseen chunks.\n"
            f"purpose={purpose}\n{content_ref}source={source}\n"
            f"chunk={index}/{total}\nchar_range={start}-{end}\n\n<chunk>\n{text}\n</chunk>"
        )

    @staticmethod
    def _reduce_prompt(
        sections: list[str],
        *,
        purpose: str,
        content_id: str,
        source: str,
        final: bool,
    ) -> str:
        content_ref = f"content_id={content_id}\n" if content_id else ""
        action = "Create the final detailed continuation summary" if final else "Merge these summaries without losing evidence"
        return (
            f"{action}. Remove duplication but preserve disagreements, errors, references, offsets and next actions.\n"
            f"purpose={purpose}\n{content_ref}source={source}\n\n<summaries>\n"
            + "\n\n".join(sections)
            + "\n</summaries>"
        )

    @staticmethod
    def _append_image_note(result: CompressionResult, note: str) -> None:
        clean = str(note or "").strip()
        if not clean or not result.summary:
            return
        available = max(0, MAX_SUMMARY_CHARS - len(clean) - 2)
        result.summary = f"{result.summary[:available].rstrip()}\n\n{clean}".strip()
        result.token_estimate = estimate_tokens(result.summary)

    def _record_trace(
        self,
        result: CompressionResult,
        *,
        input_chars: int,
        content_id: str,
        purpose: str,
        started_at: float = 0.0,
    ) -> None:
        trace = self.debug_trace
        if not getattr(trace, "enabled", False):
            return
        try:
            trace.record_event(
                kind="condense",
                phase="end",
                name=purpose,
                status="completed" if result.summary else "failed",
                duration_ms=max(0, int((time.monotonic() - started_at) * 1000)) if started_at else 0,
                summary=(
                    f"strategy={result.strategy or '-'}, chunks={result.chunks}, "
                    f"calls={result.calls}, output={len(result.summary)}"
                ),
                refs={"content_id": content_id} if content_id else None,
                data={
                    "input_chars": int(input_chars or 0),
                    "output_chars": len(result.summary),
                    "calls": int(result.calls or 0),
                    "chunks": int(result.chunks or 0),
                    "reduce_levels": int(result.reduce_levels or 0),
                    "strategy": str(result.strategy or ""),
                    "model": str(result.model or ""),
                    "vision_fallback": bool(result.vision_fallback),
                    "fallback": result.status == "fallback",
                    "fallback_reason": str(result.error or ""),
                    "error": str(result.error or ""),
                },
            )
        except Exception:
            return

    def _store(self, conversation: Conversation | None) -> SessionArchiveStore:
        if self.store is not None:
            return self.store
        return SessionArchiveStore(getattr(conversation, "work_dir", "") or ".", getattr(conversation, "id", None))

    def _capability(self, capability_id: str) -> CapabilityConfig:
        if self.capability_executor is None:
            raise ValueError(f"Missing capability executor for: {capability_id}")
        return self.capability_executor.get_capability(capability_id)

    @staticmethod
    def _parse_model_result(content: str, *, max_chars: int = MAX_SUMMARY_CHARS) -> CompressionResult:
        try:
            payload = json.loads(str(content or "").strip())
        except Exception:
            return CompressionResult(status="error", error="model_output_not_json", token_estimate=estimate_tokens(content))
        if not isinstance(payload, dict) or set(payload) != {"summary"}:
            return CompressionResult(status="error", error="model_output_invalid_schema", token_estimate=estimate_tokens(content))
        summary = str(payload.get("summary") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not summary:
            return CompressionResult(status="empty", error="summary_empty")
        if len(summary) > max(1, int(max_chars or 0)):
            return CompressionResult(status="error", error="summary_too_long", token_estimate=estimate_tokens(summary))
        return CompressionResult(summary=summary, status="complete", token_estimate=estimate_tokens(summary))
