from __future__ import annotations

from core.content.markdown import trim_text
from core.prompts.providers.base import MessageProviderMixin, ProviderContext, context_item


class ArchiveIndexProvider(MessageProviderMixin):
    name = "archive"
    priority = 40

    MAX_RECORDS = 12

    def build_items(self, context: ProviderContext):
        try:
            state = context.conversation.get_state()
            records = list((getattr(state, "archive_index", {}) or {}).values())
        except Exception:
            records = []
        if not records:
            return []

        records.sort(key=lambda item: int(getattr(item, "updated_seq", 0) or getattr(item, "created_seq", 0) or 0), reverse=True)
        by_kind: dict[str, list] = {}
        for record in records[: self.MAX_RECORDS]:
            by_kind.setdefault(str(getattr(record, "kind", "") or "tool_call"), []).append(record)

        lines = [
            "<archive_index>",
            "policy: archive index + derived views only; raw original is never injected",
            "read_rule: Use content__read(content_id, view=\"summary\"|\"full\"|\"lines\"|\"chars\") for archived PyCat content. view=\"summary\" waits for or creates the internal compress view by default. Use file__read only for real workspace files.",
            "view_labels: [full], [line:1-200], and [char:0-4000] are exact original views; [summary] is balanced derived view; [summary:*] are specialized derived internal compress views; [summary:pending] means only the summary is unavailable, not the original.",
            "summary_modes: content__read(view=\"summary\", summary_mode=\"balanced\"|\"brief\"|\"detailed\"|\"timeline\"|\"evidence\"|\"topic\"|\"memory_candidates\", topic=\"...\")",
            "summary_rule: views.summary is created by runtime internal compress. capability__summarize is only for explicit model-requested single-source summaries.",
        ]
        for kind in sorted(by_kind):
            lines.append(f"- {kind}/")
            for record in by_kind[kind]:
                lines.extend(self._record_lines(record))
        if len(records) > self.MAX_RECORDS:
            lines.append(f"- older/ ... {len(records) - self.MAX_RECORDS} older archive records omitted from prompt index")
        lines.append("dedupe_rule: use content_id/digest; cite content_id when relying on archived content.")
        lines.append("</archive_index>")
        return [
            context_item(
                "\n".join(lines),
                kind=self.name,
                priority=self.priority,
                item_id="archive:index",
                source_ref="SessionState.archive_index",
            )
        ]

    @staticmethod
    def _record_lines(record) -> list[str]:
        content_id = str(getattr(record, "id", "") or "")
        title = str(getattr(record, "title", "") or "content")
        kind = str(getattr(record, "kind", "") or "tool_call")
        source = str(getattr(record, "source", "") or "")
        original_ref = str(getattr(record, "original_ref", "") or "")
        digest = str(getattr(record, "digest", "") or "")[:16]
        chars = int(getattr(record, "size", 0) or 0)
        tokens = int(getattr(record, "token_estimate", 0) or 0)
        status = str(getattr(record, "status", "") or "")
        summary = trim_text(str(getattr(record, "summary", "") or ""), 360)
        summary_status = getattr(record, "summary_status", "pending")
        line = f'  - {content_id}/ title="{title}" kind={kind} source={source or "-"} chars={chars} tokens~{tokens} digest={digest}'
        lines = [line, f"    original_ref: {original_ref}"]
        lines.append(f"    views: full=available; summary={summary_status}")
        if status:
            lines.append(f"    status: {status}")
        if summary:
            lines.append(f"    summary: {summary}")
        metadata = getattr(record, "metadata", {}) or {}
        refs = [str(item).strip() for item in (metadata.get("references") or []) if str(item).strip()]
        if refs:
            lines.append("    references: " + " | ".join(refs[:4]))
        lines.append(f"    read: content__read(content_id=\"{content_id}\", view=\"summary\")")
        return lines
