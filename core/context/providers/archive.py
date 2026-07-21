from __future__ import annotations

from core.content.markdown import trim_text
from core.context.items import ContextItem
from core.llm.token_budget import estimate_tokens
from core.context.providers.base import MessageProviderMixin, ProviderContext, context_item


class ArchiveIndexProvider(MessageProviderMixin):
    name = "archive"
    priority = 40

    MAX_RECORDS = 12
    TOKEN_BUDGET = 2_500

    def build_items(self, context: ProviderContext):
        try:
            state = context.conversation.get_state()
            records = list((getattr(state, "archive_index", {}) or {}).values())
        except Exception:
            records = []
        if not records:
            return []

        records.sort(key=lambda item: int(getattr(item, "updated_seq", 0) or getattr(item, "created_seq", 0) or 0), reverse=True)
        token_budget = self.TOKEN_BUDGET
        records = self._records_within_budget(records, token_budget)

        lines = [
            "<archive_index>",
            "Recover exact text with archive__read(content_id, view=\"content\", offset=...).",
        ]
        for record in records:
            lines.extend(self._record_lines(record))
        total_count = self._record_count(context)
        if total_count > len(records):
            lines.append(f"- older/ ... {total_count - len(records)} older archive records omitted from prompt index")
        lines.append("</archive_index>")
        return [
            context_item(
                "\n".join(lines),
                kind=self.name,
                priority=self.priority,
                item_id="archive:index",
                source_ref="SessionState.archive_index",
                metadata={"token_budget": token_budget, "records_rendered": len(records), "records_total": total_count},
                degrade_fn=self._degrade_archive_item,
            )
        ]

    @staticmethod
    def _record_lines(record) -> list[str]:
        content_id = str(getattr(record, "id", "") or "")
        source = str(getattr(record, "source", "") or "")
        chars = int(getattr(record, "size", 0) or 0)
        summary = trim_text(str(getattr(record, "summary", "") or ""), 360)
        lines = [f"- content_id={content_id} source={source or '-'} chars={chars}"]
        if summary:
            lines.append(f"  summary={summary}")
        return lines

    @staticmethod
    def _record_count(context: ProviderContext) -> int:
        try:
            state = context.conversation.get_state()
            return len(getattr(state, "archive_index", {}) or {})
        except Exception:
            return 0

    def _records_within_budget(self, records: list, token_budget: int) -> list:
        selected = []
        header_tokens = 180
        used = header_tokens
        for record in records[: self.MAX_RECORDS]:
            record_text = "\n".join(self._record_lines(record))
            tokens = estimate_tokens(record_text)
            if selected and used + tokens > token_budget:
                break
            selected.append(record)
            used += tokens
        return selected

    @staticmethod
    def _degrade_archive_item(item: ContextItem, remaining_tokens: int) -> ContextItem | None:
        if remaining_tokens <= 120:
            return None
        lines = str(item.content or "").splitlines()
        kept: list[str] = []
        used = 0
        for line in lines:
            line_tokens = estimate_tokens(line)
            if kept and used + line_tokens > remaining_tokens:
                break
            kept.append(line)
            used += line_tokens
        if not kept:
            return None
        if kept[-1].strip() != "</archive_index>":
            kept.append("- omitted: archive index truncated by request budget")
            kept.append("</archive_index>")
        content = "\n".join(kept)
        return ContextItem(
            id=item.id,
            kind=item.kind,
            content=content,
            priority=item.priority,
            required=item.required,
            source_ref=item.source_ref,
            relevance_score=item.relevance_score,
            freshness=item.freshness,
            metadata={**dict(item.metadata or {}), "degraded": True},
            token_estimate=estimate_tokens(content),
            degrade_fn=item.degrade_fn,
        )
