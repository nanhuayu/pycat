from __future__ import annotations

import json
import logging
from html import escape
from typing import Any

from core.capabilities import CapabilityRunContext
from core.llm.structured_output import load_json_object, one_line, string_list
from core.memory.service import MemoryService, MemorySnippet
from models.contracts.session_state import MEMORY_CATEGORIES, MEMORY_SCOPES, TodoStatus
from models.conversation import Conversation, Message
from models.provider import Provider


logger = logging.getLogger(__name__)


class MemoryAdvisor:
    """Bounded, tool-free memory advice and run-end candidate curation."""

    ADVICE_LIMIT = 5
    CANDIDATE_LIMIT = 6

    def __init__(self, capability_executor: Any) -> None:
        self._executor = capability_executor

    async def advise_current(
        self,
        *,
        provider: Provider,
        conversation: Conversation,
        debug_trace: Any = None,
    ) -> str:
        """Build advice once at the real-user boundary of an Agent run."""
        try:
            state = conversation.get_state()
            todo = next(
                (item for item in (state.todos or []) if item.status == TodoStatus.IN_PROGRESS),
                None,
            )
        except Exception:
            todo = None
        if todo is None:
            return ""
        return await self.advise(
            provider=provider,
            conversation=conversation,
            todo=todo,
            query=self._latest_user_query(conversation),
            sources=(conversation.settings or {}).get("memory_sources"),
            debug_trace=debug_trace,
        )

    async def advise(
        self,
        *,
        provider: Provider,
        conversation: Conversation,
        todo: Any,
        query: str,
        sources: Any = None,
        debug_trace: Any = None,
    ) -> str:
        if not self._capability_enabled("memory_advise"):
            return ""
        state = conversation.get_state()
        memory_query = "\n".join(
            part
            for part in (
                str(query or "").strip(),
                str(getattr(todo, "title", "") or "").strip(),
                str(getattr(todo, "note", "") or "").strip(),
            )
            if part
        )
        snippets = MemoryService.select_relevant(
            state,
            memory_query,
            limit=8,
            work_dir=getattr(conversation, "work_dir", ".") or ".",
            sources=sources,
        )
        if not snippets:
            return ""

        catalog = self._catalog(snippets)
        payload = {
            "milestone": {
                "id": str(getattr(todo, "id", "") or ""),
                "title": str(getattr(todo, "title", "") or ""),
                "note": str(getattr(todo, "note", "") or ""),
                "refs": list(getattr(todo, "refs", []) or [])[:12],
            },
            "current_request": one_line(query, 1200),
            "memory_catalog": catalog,
        }
        try:
            result = await self._executor.run_capability(
                provider=provider,
                capability_id="memory_advise",
                message=json.dumps(payload, ensure_ascii=False),
                conversation=conversation,
                context=CapabilityRunContext(
                    caller="runtime",
                    source="memory",
                    entrypoint="milestone",
                    debug_trace=self._memory_trace(debug_trace),
                ),
                title="Memory advice",
            )
            parsed = result.parsed if isinstance(getattr(result, "parsed", None), dict) else load_json_object(result.content)
            content = self._render_advice(parsed, catalog)
        except Exception as exc:
            logger.debug("Memory advice failed: %s", exc)
            content = ""
        return content

    async def curate(
        self,
        *,
        provider: Provider,
        conversation: Conversation,
        final_message: Message | None,
        debug_trace: Any = None,
    ) -> int:
        if not self._capability_enabled("memory_curate"):
            return 0
        state = conversation.get_state()
        review_seq = int(state.last_memory_review_seq or 0)
        completed = [
            item
            for item in (state.recent_completed_todos or [])
            if item.status == TodoStatus.COMPLETED and int(item.completed_seq or 0) > review_seq
        ]
        artifacts = [
            artifact
            for artifact in (state.artifacts or {}).values()
            if str(getattr(artifact, "status", "") or "").strip().lower() in {"final", "approved"}
            and int(getattr(artifact, "updated_seq", 0) or 0) > review_seq
        ]
        if not completed and not artifacts:
            return 0

        signal_seq = max(
            [review_seq]
            + [int(item.completed_seq or 0) for item in completed]
            + [int(getattr(item, "updated_seq", 0) or 0) for item in artifacts]
        )
        allowed_refs = self._allowed_refs(completed, artifacts)
        signal_text = "\n".join(
            [item.title for item in completed]
            + [str(getattr(item, "abstract", "") or getattr(item, "name", "") or "") for item in artifacts]
        )
        existing = MemoryService.select_relevant(
            state,
            signal_text,
            limit=10,
            work_dir=getattr(conversation, "work_dir", ".") or ".",
        )
        payload = {
            "completed_milestones": [
                {
                    "ref": self._todo_ref(item),
                    "title": item.title,
                    "note": item.note,
                    "refs": list(item.refs or [])[:12],
                }
                for item in completed
            ],
            "final_artifacts": [
                {
                    "ref": f"artifact:{getattr(item, 'name', '')}",
                    "name": str(getattr(item, "name", "") or ""),
                    "kind": str(getattr(item, "kind", "") or ""),
                    "abstract": one_line(getattr(item, "abstract", ""), 1000),
                    "refs": list(getattr(item, "references", []) or [])[:12],
                }
                for item in artifacts
            ],
            "final_outcome": one_line(getattr(final_message, "content", ""), 3000),
            "existing_memory": self._catalog(existing),
            "allowed_refs": sorted(allowed_refs),
        }
        try:
            result = await self._executor.run_capability(
                provider=provider,
                capability_id="memory_curate",
                message=json.dumps(payload, ensure_ascii=False),
                conversation=conversation,
                context=CapabilityRunContext(
                    caller="runtime",
                    source="memory",
                    entrypoint="run_complete",
                    debug_trace=self._memory_trace(debug_trace),
                ),
                title="Memory review",
            )
            parsed = result.parsed if isinstance(getattr(result, "parsed", None), dict) else load_json_object(result.content)
            candidates = self._parse_candidates(parsed.get("candidates") if isinstance(parsed, dict) else None, allowed_refs)
        except Exception as exc:
            logger.debug("Memory curation failed: %s", exc)
            return 0

        added = MemoryService.add_memory_candidates(state, candidates, current_seq=signal_seq)
        state.last_memory_review_seq = signal_seq
        state.last_updated_seq = max(int(state.last_updated_seq or 0), signal_seq)
        state.state_version += 1
        conversation.set_state(state)
        return added

    @staticmethod
    def _catalog(snippets: list[MemorySnippet]) -> list[dict[str, Any]]:
        return [
            {
                "id": f"M{index}",
                "scope": item.scope,
                "key": item.read_key or item.key,
                "source": item.source,
                "preview": one_line(item.value, 900),
            }
            for index, item in enumerate(snippets, start=1)
        ]

    @staticmethod
    def _latest_user_query(conversation: Conversation) -> str:
        for message in reversed(getattr(conversation, "messages", []) or []):
            if getattr(message, "role", "") != "user":
                continue
            content = str(getattr(message, "content", "") or "").strip()
            if content:
                return content
        return ""

    @classmethod
    def _render_advice(cls, parsed: Any, catalog: list[dict[str, Any]]) -> str:
        if not isinstance(parsed, dict):
            return ""
        source_map = {str(item.get("id") or ""): item for item in catalog}
        lines: list[str] = []
        cited: list[str] = []
        for item in parsed.get("advice") or []:
            if not isinstance(item, dict):
                continue
            text = escape(one_line(item.get("text"), 500), quote=False)
            source_ids = [
                source_id
                for source_id in string_list(item.get("source_ids"), limit=5, item_limit=20)
                if source_id in source_map
            ]
            if not text or not source_ids:
                continue
            lines.append(f"- {text}")
            lines.append(f"  sources: {', '.join(source_ids)}")
            verify = escape(one_line(item.get("verify"), 300), quote=False)
            if verify:
                lines.append(f"  verify: {verify}")
            cited.extend(source_id for source_id in source_ids if source_id not in cited)
            if len([line for line in lines if line.startswith("- ")]) >= cls.ADVICE_LIMIT:
                break
        if not lines:
            return ""
        source_lines = [
            f"- {source_id}: state__memory(action=\"view\", scope={json.dumps(source_map[source_id].get('scope'))}, "
            f"key={json.dumps(source_map[source_id].get('key'))})"
            for source_id in cited
        ]
        return "\n".join([
            '<memory_advice advisory="true">',
            *lines,
            "source_catalog:",
            *source_lines,
            "rule: verify memory against the current request and workspace evidence.",
            "</memory_advice>",
        ])

    def _capability_enabled(self, capability_id: str) -> bool:
        config = getattr(self._executor, "capabilities", None)
        capability = config.capability(capability_id) if config is not None and hasattr(config, "capability") else None
        return capability is None or bool(getattr(capability, "enabled", True))

    @staticmethod
    def _memory_trace(debug_trace: Any) -> Any:
        if debug_trace is not None and hasattr(debug_trace, "with_purpose"):
            return debug_trace.with_purpose("memory")
        return debug_trace

    @classmethod
    def _parse_candidates(cls, value: Any, allowed_refs: set[str]) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        candidates: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            content = one_line(item.get("content"), 600)
            scope = str(item.get("scope") or "session").strip().lower()
            category = str(item.get("category") or "fact").strip().lower()
            refs = [ref for ref in string_list(item.get("refs"), limit=12, item_limit=500) if ref in allowed_refs]
            if not content or not refs:
                continue
            candidates.append(
                {
                    "content": content,
                    "scope": scope if scope in MEMORY_SCOPES else "session",
                    "category": category if category in MEMORY_CATEGORIES else "fact",
                    "reason": one_line(item.get("reason"), 600),
                    "refs": refs,
                }
            )
            if len(candidates) >= cls.CANDIDATE_LIMIT:
                break
        return candidates

    @staticmethod
    def _todo_ref(item: Any) -> str:
        todo_id = str(getattr(item, "id", "") or "").strip()
        return f"todo:{todo_id}" if todo_id else f"todo:{one_line(getattr(item, 'title', ''), 120)}"

    @classmethod
    def _allowed_refs(cls, completed: list[Any], artifacts: list[Any]) -> set[str]:
        refs: set[str] = set()
        for item in completed:
            refs.add(cls._todo_ref(item))
            refs.update(str(ref).strip() for ref in (getattr(item, "refs", []) or []) if str(ref).strip())
        for item in artifacts:
            refs.add(f"artifact:{getattr(item, 'name', '')}")
            refs.update(str(ref).strip() for ref in (getattr(item, "references", []) or []) if str(ref).strip())
        return refs
