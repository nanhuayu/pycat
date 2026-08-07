"""Capability-backed, model-aware context compression."""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace
from typing import Any

from core.capabilities import CapabilityConfig
from core.context.compression import (
    COMPRESSION_IMAGE_TOKEN_RESERVE,
    COMPRESSION_INPUT_SAFETY_RATIO,
    CompressionPurpose,
    CompressionResult,
    compression_chunk_prompt,
    compression_document_prompt,
    compression_reduce_prompt,
    compression_system_contract,
    group_sections_by_token_budget,
    parse_compression_result,
    split_text_by_token_budget,
)
from core.llm.model_selection import ResolvedModelSelection
from core.llm.token_budget import estimate_tokens, resolve_token_budget
from models.conversation import Conversation
from models.provider import Provider


logger = logging.getLogger(__name__)
MAX_REDUCE_LEVELS = 8


class CapabilityCompressor:
    def __init__(
        self,
        provider: Provider,
        *,
        capability_executor: Any,
        debug_trace: Any = None,
    ) -> None:
        self.provider = provider
        self.debug_trace = debug_trace
        self.capability_executor = capability_executor

    async def compress(
        self,
        text: str,
        *,
        purpose: CompressionPurpose,
        images: list[str] | None = None,
        conversation: Conversation | None = None,
        trace_purpose: str = "",
        content_id: str = "",
    ) -> CompressionResult:
        compression_system_contract(purpose)
        started_at = time.monotonic()
        trace_name = str(trace_purpose or purpose)
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
            purpose=purpose,
            image_count=len(usable_images),
        )
        full_prompt = compression_document_prompt(
            text,
            purpose=purpose,
        )
        if estimate_tokens(full_prompt) <= prompt_budget:
            result = await self._run_capability(
                capability,
                full_prompt,
                conversation=conversation,
                selection=selection,
                images=usable_images,
                purpose=purpose,
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
                purpose=trace_name,
                started_at=started_at,
            )
            return result

        chunk_overhead = estimate_tokens(
            compression_chunk_prompt(
                "",
                purpose=purpose,
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
                purpose=trace_name,
                started_at=started_at,
            )
            return failed
        chunks = split_text_by_token_budget(text, body_budget)
        partials: list[str] = []
        calls = 0
        for index, chunk in enumerate(chunks, start=1):
            prompt = compression_chunk_prompt(
                chunk.text,
                purpose=purpose,
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
                purpose=purpose,
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
                    purpose=trace_name,
                    started_at=started_at,
                )
                return result
            partials.append(
                f"## Chunk {index}/{len(chunks)} char_range={chunk.start}-{chunk.end}\n{result.summary}"
            )

        reduce_levels = 0
        sections = partials
        while reduce_levels < MAX_REDUCE_LEVELS:
            final_prompt = compression_reduce_prompt(
                sections,
                purpose=purpose,
                final=True,
            )
            if estimate_tokens(final_prompt) <= prompt_budget:
                final = await self._run_capability(
                    capability,
                    final_prompt,
                    conversation=conversation,
                    selection=selection,
                    images=[],
                    purpose=purpose,
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
                    purpose=trace_name,
                    started_at=started_at,
                )
                return final

            reduce_levels += 1
            reduce_overhead = estimate_tokens(
                compression_reduce_prompt(
                    [],
                    purpose=purpose,
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
                prompt = compression_reduce_prompt(
                    group,
                    purpose=purpose,
                    final=False,
                )
                result = await self._run_capability(
                    capability,
                    prompt,
                    conversation=conversation,
                    selection=selection,
                    images=[],
                    purpose=purpose,
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
                        purpose=trace_name,
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
            purpose=trace_name,
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
        purpose: CompressionPurpose,
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
                extra_system_contract=compression_system_contract(purpose),
                title=f"runtime_{capability.id}_compression",
                images=list(images or []),
                resolved_selection=selection,
            )
        except Exception as exc:
            logger.warning("Capability compression failed via %s: %s", capability.id, exc)
            return CompressionResult(status="error", capability_id=capability.id, error=str(exc))

        result = parse_compression_result(getattr(response, "content", ""))
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
        purpose: CompressionPurpose,
        image_count: int,
    ) -> int:
        provider = selection.provider or self.provider
        budget = resolve_token_budget(provider=provider, model_id=selection.model)
        effective = int(budget.effective_prompt_limit or budget.context_window or 0)
        safe = int(effective * COMPRESSION_INPUT_SAFETY_RATIO)
        overhead = estimate_tokens(str(capability.prompt or "")) + estimate_tokens(
            compression_system_contract(purpose)
        )
        overhead += max(0, int(image_count or 0)) * COMPRESSION_IMAGE_TOKEN_RESERVE
        return max(1, safe - overhead)

    @staticmethod
    def _supports_vision(selection: ResolvedModelSelection) -> bool:
        provider = selection.provider
        if provider is None:
            return False
        try:
            return provider.effective_model_profile(selection.model).supports_input("image")
        except Exception:
            return False

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
    def _append_image_note(result: CompressionResult, note: str) -> None:
        clean = str(note or "").strip()
        if not clean or not result.summary:
            return
        result.summary = f"{result.summary.rstrip()}\n\n{clean}".strip()
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

    def _capability(self, capability_id: str) -> CapabilityConfig:
        if self.capability_executor is None:
            raise ValueError(f"Missing capability executor for: {capability_id}")
        return self.capability_executor.get_capability(capability_id)
