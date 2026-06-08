"""LLM-backed runtime compression for archived content and old history."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from core.capabilities import CapabilityConfig, default_capabilities_config
from core.context.archive_store import ArchivedContentRecord, SessionArchiveStore
from core.llm.structured_output import confidence as parse_confidence
from core.llm.structured_output import load_json_object, one_line, string_list
from core.llm.token_budget import estimate_tokens
from core.runtime.agent_runtime import AgentRuntime, RuntimeCallContext
from models.conversation import Conversation, Message, normalize_tool_result, tool_call_name
from models.provider import Provider
from models.state import SessionState

logger = logging.getLogger(__name__)


JSON_OUTPUT_CONTRACT = """Return exactly one valid JSON object with this schema:
{
  "summary_brief": "very short Chinese summary, 80-240 chars",
  "summary": "balanced dense Chinese summary, 200-1200 chars",
  "summary_detailed": "detail-preserving Chinese summary, 800-3000 chars when useful",
  "key_points": ["important point, decision, claim, finding, or constraint"],
  "timeline_items": ["time-bound event, if any"],
  "entities": ["important person, organization, file path, symbol, product, place, or system"],
  "claims": ["source-grounded claim or conclusion"],
  "references": ["URL, file path, archive content_id, citation, or source identifier"],
  "sections": [{"title": "section name", "items": ["concise item"]}],
  "memory_candidates": [{"content": "short durable reusable fact", "category": "preference|fact|decision|convention|command|gotcha", "confidence": 0.0, "evidence_refs": ["source id"]}],
  "confidence": 0.0
}

Compression requirements:
- Use the supplied text as the only evidence; do not invent facts.
- Preserve provenance, source paths, URLs, dates, decisions, constraints, risks, errors, and next actions.
- For opaque command/tool output, summarize what is knowable and cite paths or digests; do not copy raw dumps.
- Memory candidates are suggestions only; runtime must not auto-write them to memory.
- Prefer Chinese output unless the source is entirely non-Chinese and no Chinese context exists.
- The response must be parseable JSON only: no Markdown fences, no commentary, no tool calls.
""".strip()


@dataclass
class CompressionResult:
    summary_brief: str = ""
    summary: str = ""
    summary_detailed: str = ""
    key_points: list[str] = field(default_factory=list)
    timeline_items: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    claims: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    sections: list[dict[str, Any]] = field(default_factory=list)
    memory_candidates: list[dict[str, Any]] = field(default_factory=list)
    confidence: float = 0.0
    status: str = "empty"
    token_estimate: int = 0
    model: str = ""
    capability_id: str = ""
    error: str = ""


class CapabilityCompressionOrchestrator:
    def __init__(self, client: Any, provider: Provider, store: SessionArchiveStore | None = None) -> None:
        self.client = client
        self.provider = provider
        self.store = store
        self.capabilities = default_capabilities_config()

    async def summarize_archive(
        self,
        record: ArchivedContentRecord,
        *,
        conversation: Conversation | None = None,
        purpose: str = "maintenance",
    ) -> CompressionResult:
        capability = self._capability("compress")
        store = self._store(conversation)
        text = store.read_original(record)
        prompt = self._archive_prompt(record, text, purpose=purpose)
        result = await self._run_capability(capability, prompt, conversation=conversation)
        refs = [str(item).strip() for item in (record.metadata.get("references") or []) if str(item).strip()]
        for ref in refs:
            if ref not in result.references:
                result.references.append(ref)
        result.capability_id = capability.id
        return result

    async def summarize_archive_topic(
        self,
        record: ArchivedContentRecord,
        *,
        mode: str,
        topic: str,
        conversation: Conversation | None = None,
    ) -> CompressionResult:
        capability = self._capability("compress")
        store = self._store(conversation)
        prompt = self._topic_prompt(record, store.read_original(record), mode=mode, topic=topic)
        result = await self._run_capability(capability, prompt, conversation=conversation)
        result.capability_id = capability.id
        return result

    async def compress_history(
        self,
        messages: Iterable[Message],
        state: SessionState | None = None,
        *,
        conversation: Conversation | None = None,
    ) -> CompressionResult:
        capability = self._capability("compress")
        prompt = self._history_prompt(messages, state)
        result = await self._run_capability(capability, prompt, conversation=conversation)
        result.capability_id = capability.id
        return result

    def apply_archive_summary(
        self,
        record: ArchivedContentRecord,
        result: CompressionResult,
        *,
        conversation: Conversation | None = None,
    ) -> ArchivedContentRecord:
        store = self._store(conversation)
        record.metadata = dict(record.metadata or {})
        record.metadata["memory_candidates"] = list(result.memory_candidates or [])[:12]
        record.metadata["key_points"] = list(result.key_points or [])[:16]
        record.metadata["timeline_items"] = list(result.timeline_items or [])[:16]
        record.metadata["entities"] = list(result.entities or [])[:24]
        record.metadata["claims"] = list(result.claims or [])[:16]
        record.metadata["references"] = list(
            dict.fromkeys(list(record.metadata.get("references") or []) + list(result.references or []))
        )[:40]
        record.metadata["sections"] = list(result.sections or [])[:12]
        record.metadata["compressed_token_estimate"] = int(result.token_estimate or 0)

        source = f"capability__{result.capability_id or 'compress'}"
        shared_metadata = {
            "key_points": result.key_points,
            "timeline_items": result.timeline_items,
            "entities": result.entities,
            "claims": result.claims,
            "references": result.references,
            "sections": result.sections,
            "memory_candidates": result.memory_candidates,
        }
        if result.summary:
            store.write_summary_view(
                record,
                summary=result.summary,
                source=source,
                model=result.model,
                confidence=result.confidence,
                metadata=shared_metadata,
            )
            self._write_derived_views(store, record, result, source=source, metadata=shared_metadata)
        else:
            store.mark_summary_failed(record, error=result.error or result.status)
        return store.read_record(record.id) or record

    def _write_derived_views(
        self,
        store: SessionArchiveStore,
        record: ArchivedContentRecord,
        result: CompressionResult,
        *,
        source: str,
        metadata: dict[str, Any],
    ) -> None:
        if result.summary_brief:
            store.write_view(
                record,
                key="summary.brief",
                label="summary:brief",
                text=result.summary_brief,
                source=source,
                model=result.model,
                confidence=result.confidence,
            )
        if result.summary_detailed:
            store.write_view(
                record,
                key="summary.detailed",
                label="summary:detailed",
                text=result.summary_detailed,
                source=source,
                model=result.model,
                confidence=result.confidence,
                metadata=metadata,
            )
        if result.timeline_items:
            store.write_view(
                record,
                key="summary.timeline",
                label="summary:timeline",
                text="\n".join(f"- {item}" for item in result.timeline_items),
                source=source,
                model=result.model,
                confidence=result.confidence,
            )
        if result.memory_candidates:
            lines: list[str] = []
            for item in result.memory_candidates[:12]:
                content = str(item.get("content") or "").strip() if isinstance(item, dict) else str(item or "").strip()
                category = str(item.get("category") or "fact").strip() if isinstance(item, dict) else "fact"
                item_confidence = item.get("confidence") if isinstance(item, dict) else ""
                if content:
                    suffix = f" confidence={item_confidence}" if item_confidence != "" else ""
                    lines.append(f"- [{category}] {content}{suffix}")
            if lines:
                store.write_view(
                    record,
                    key="summary.memory_candidates",
                    label="summary:memory_candidates",
                    text="\n".join(lines),
                    source=source,
                    model=result.model,
                    confidence=result.confidence,
                )

    async def _run_capability(
        self,
        capability: CapabilityConfig,
        user_prompt: str,
        *,
        conversation: Conversation | None,
    ) -> CompressionResult:
        try:
            response = await AgentRuntime(client=self.client).run_capability(
                provider=self.provider,
                capability_id=capability.id,
                message=user_prompt,
                conversation=conversation,
                context=RuntimeCallContext(caller="internal", source="internal", entrypoint="context_maintenance"),
                config=self.capabilities,
                extra_system_contract=JSON_OUTPUT_CONTRACT,
                title=f"runtime_{capability.id}_compression",
            )
        except Exception as exc:
            logger.warning("Capability compression failed via %s: %s", capability.id, exc)
            return CompressionResult(status="error", capability_id=capability.id, error=str(exc))

        result = self._parse_model_result(response.content)
        result.model = response.model
        result.capability_id = capability.id
        return result

    def _store(self, conversation: Conversation | None) -> SessionArchiveStore:
        if self.store is not None:
            return self.store
        return SessionArchiveStore(getattr(conversation, "work_dir", "") or ".", getattr(conversation, "id", None))

    def _capability(self, capability_id: str) -> CapabilityConfig:
        cap = self.capabilities.capability(capability_id)
        if cap is None:
            raise ValueError(f"Missing built-in capability: {capability_id}")
        return cap

    def _archive_prompt(self, record: ArchivedContentRecord, text: str, *, purpose: str) -> str:
        metadata = {
            "purpose": purpose,
            "content_id": record.id,
            "kind": record.kind,
            "title": record.title,
            "source": record.source,
            "original_ref": record.original_ref,
            "digest": record.digest,
            "size": record.size,
            "token_estimate": record.token_estimate,
            "metadata": dict(record.metadata or {}),
        }
        return (
            "请压缩下面这一条归档内容或工具结果，并且只返回符合系统约定的 JSON。\n"
            "压缩是原文的派生视图，不能替代原文。必须保留可恢复引用、来源、路径、URL、日期、结论、限制、错误和下一步；不要编造信息。\n\n"
            "## Archive metadata\n"
            f"{json.dumps(metadata, ensure_ascii=False, indent=2)}\n\n"
            "## Full original content\n"
            f"{text}"
        )

    def _topic_prompt(self, record: ArchivedContentRecord, text: str, *, mode: str, topic: str) -> str:
        metadata = {
            "content_id": record.id,
            "kind": record.kind,
            "title": record.title,
            "source": record.source,
            "original_ref": record.original_ref,
            "digest": record.digest,
            "summary_mode": mode,
            "topic": topic,
        }
        focus = "和主题直接相关的事实、证据、日期、来源、不确定性、反例和可引用片段"
        if mode == "evidence":
            focus = "能支持或反驳主题的证据、来源、引用、日期、置信度和不确定性"
        return (
            "请基于归档原文生成一个主题定向派生 summary view，并只返回符合系统约定的 JSON。\n"
            f"聚焦范围：{focus}。不得使用原文之外的信息，不得编造。\n\n"
            "## Topic metadata\n"
            f"{json.dumps(metadata, ensure_ascii=False, indent=2)}\n\n"
            "## Full original content\n"
            f"{text}"
        )

    def _history_prompt(self, messages: Iterable[Message], state: SessionState | None) -> str:
        transcript: list[dict[str, Any]] = []
        for msg in messages:
            item: dict[str, Any] = {
                "seq_id": getattr(msg, "seq_id", 0),
                "role": getattr(msg, "role", "message"),
                "content": getattr(msg, "summary", None) or getattr(msg, "content", "") or "",
            }
            tool_items: list[dict[str, Any]] = []
            for tc in getattr(msg, "tool_calls", None) or []:
                if not isinstance(tc, dict):
                    continue
                result = normalize_tool_result(tc.get("result")) if tc.get("result") is not None else {}
                meta = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
                tool_items.append(
                    {
                        "name": tool_call_name(tc) or str(meta.get("name") or "tool"),
                        "summary": tc.get("result_summary") or result.get("summary") or meta.get("tool_result_summary") or "",
                        "content_id": meta.get("content_id") or "",
                        "digest": meta.get("tool_result_digest") or "",
                    }
                )
            if tool_items:
                item["tools"] = tool_items
            transcript.append(item)

        archive: list[dict[str, Any]] = []
        if state and state.archive_index:
            for record in state.archive_index.values():
                archive.append(
                    {
                        "content_id": record.id,
                        "title": record.title,
                        "source": record.source,
                        "summary": record.summary,
                        "status": record.status,
                        "digest": record.digest,
                    }
                )
        payload = {
            "previous_summary": getattr(state, "summary", "") if state is not None else "",
            "messages": transcript,
            "archive": archive,
        }
        return (
            "请压缩以下会话历史，保留用户目标、已完成结论、关键证据、未决事项和可恢复的归档引用。"
            "不要写逐工具流水账，不要把临时搜索事实当成长期记忆。只返回符合系统约定的 JSON。\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        )

    def _parse_model_result(self, content: str) -> CompressionResult:
        payload = load_json_object(content)
        if not isinstance(payload, dict):
            return CompressionResult(status="error", error="model_output_not_json", token_estimate=estimate_tokens(content))
        summary_brief = one_line(payload.get("summary_brief"), 600)
        summary = one_line(payload.get("summary"), 2000)
        summary_detailed = one_line(payload.get("summary_detailed"), 5000)
        key_points = string_list(payload.get("key_points"), limit=16, item_limit=500)
        timeline = string_list(payload.get("timeline_items"), limit=16, item_limit=500)
        entities = string_list(payload.get("entities"), limit=24, item_limit=200)
        claims = string_list(payload.get("claims"), limit=16, item_limit=500)
        refs = string_list(payload.get("references"), limit=40, item_limit=500)
        sections = self._sections(payload.get("sections"))
        memory_candidates = self._memory_candidates(payload.get("memory_candidates"), refs)
        confidence = parse_confidence(payload.get("confidence"))
        return CompressionResult(
            summary_brief=summary_brief,
            summary=summary,
            summary_detailed=summary_detailed,
            key_points=key_points,
            timeline_items=timeline,
            entities=entities,
            claims=claims,
            references=refs,
            sections=sections,
            memory_candidates=memory_candidates,
            confidence=confidence,
            status="complete" if summary else "empty",
            token_estimate=estimate_tokens(summary),
        )

    @staticmethod
    def _sections(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        sections: list[dict[str, Any]] = []
        for item in value[:12]:
            if not isinstance(item, dict):
                continue
            title = one_line(item.get("title"), 80) or "section"
            section_items = string_list(item.get("items"), limit=20, item_limit=500)
            sections.append({"title": title, "items": section_items})
        return sections

    @staticmethod
    def _memory_candidates(value: Any, refs: list[str]) -> list[dict[str, Any]]:
        raw_items = value if isinstance(value, list) else []
        candidates: list[dict[str, Any]] = []
        allowed_categories = {"preference", "fact", "decision", "convention", "command", "gotcha"}
        for item in raw_items[:16]:
            if isinstance(item, dict):
                content = one_line(item.get("content") or item.get("fact") or item.get("text"), 600)
                category = str(item.get("category") or "fact").strip().lower()
                evidence_refs = string_list(item.get("evidence_refs"), limit=8, item_limit=300)
                confidence = parse_confidence(item.get("confidence"))
            else:
                content = one_line(item, 600)
                category = "fact"
                evidence_refs = []
                confidence = 0.5
            if not content:
                continue
            if category not in allowed_categories:
                category = "fact"
            if not evidence_refs:
                evidence_refs = list(refs[:4])
            candidates.append(
                {
                    "content": content,
                    "category": category,
                    "confidence": confidence,
                    "evidence_refs": evidence_refs,
                }
            )
        return candidates[:12]
