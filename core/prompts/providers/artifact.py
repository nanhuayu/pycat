from __future__ import annotations

from core.content.markdown import trim_text
from core.prompts.providers.base import MessageProviderMixin, ProviderContext, context_item


class ArtifactProvider(MessageProviderMixin):
    name = "artifact"
    priority = 35

    def build_items(self, context: ProviderContext):
        mode_slug = str(getattr(context.conversation, "mode", "chat") or "chat").strip().lower()
        try:
            state = context.conversation.get_state()
            artifacts = getattr(state, "artifacts", {}) or {}
        except Exception:
            artifacts = {}
        grouped: dict[str, list[str]] = {}
        for name, artifact in artifacts.items():
            normalized = str(name or "").strip().lower()
            if not normalized:
                continue
            preview_source = str(getattr(artifact, "abstract", "") or "").strip()
            if not preview_source:
                continue
            kind = str(getattr(artifact, "kind", "") or "").strip().lower()
            status = str(getattr(artifact, "status", "") or "").strip().lower()
            references = [str(item).strip() for item in (getattr(artifact, "references", []) or []) if str(item).strip()]
            related = [str(item).strip() for item in (getattr(artifact, "related", []) or []) if str(item).strip()]
            label_parts = []
            if kind:
                label_parts.append(kind)
            if status:
                label_parts.append(f"status={status}")
            label = f" [{' | '.join(label_parts)}]" if label_parts else ""
            parts = [f"  - {normalized}/{label}: {trim_text(preview_source, 300)}"]
            content_path = str(getattr(artifact, "content_path", "") or "").strip()
            content_digest = str(getattr(artifact, "content_digest", "") or "").strip()[:16]
            content_chars = int(getattr(artifact, "content_chars", 0) or 0)
            if content_path:
                parts.append(f"path={content_path}")
            if content_digest:
                parts.append(f"digest={content_digest}")
            if content_chars:
                parts.append(f"chars={content_chars}")
            if references:
                parts.append(f"refs={', '.join(references[:3])}")
            if related:
                parts.append(f"related={', '.join(related[:3])}")
            grouped.setdefault(kind or "artifact", []).append(" | ".join(parts))
        if not grouped:
            return []

        suffix = ""
        if mode_slug in {"agent", "code", "debug", "plan", "orchestrator", "explore"}:
            suffix = "\nread_rule: Full artifact content is not injected. Use `state__artifact(action=\"read\")` when exact content is needed; keep abstract/status/references updated after changing artifacts."
        lines = [
            "<session_artifacts>",
            "policy: tree index + abstract + status + path + digest + references; no raw artifact body",
            "maintenance_rule: keep abstract/status/references/related current after creating or updating artifacts.",
        ]
        for kind in sorted(grouped):
            lines.append(f"- {kind}/")
            lines.extend(grouped[kind])
        content = "\n".join(lines) + suffix + "\n</session_artifacts>"
        return [context_item(content, kind=self.name, priority=self.priority, item_id="artifact:index", source_ref="SessionState.artifacts")]
