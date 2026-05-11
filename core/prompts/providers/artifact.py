from __future__ import annotations

from core.content.markdown import trim_text
from core.prompts.providers.base import ProviderContext, synthetic_context_message


class ArtifactProvider:
    name = "artifact"
    priority = 35

    def build(self, context: ProviderContext):
        mode_slug = str(getattr(context.conversation, "mode", "chat") or "chat").strip().lower()
        try:
            state = context.conversation.get_state()
            artifacts = getattr(state, "artifacts", {}) or {}
        except Exception:
            artifacts = {}
        lines: list[str] = []
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
            parts = [f"- {normalized}{label}: {trim_text(preview_source, 300)}"]
            if references:
                parts.append(f"refs={', '.join(references[:3])}")
            if related:
                parts.append(f"related={', '.join(related[:3])}")
            lines.append(" | ".join(parts))
        if not lines:
            return []

        suffix = ""
        if mode_slug in {"agent", "code", "debug", "plan", "orchestrator", "explore"}:
            suffix = "\nFull artifact content is not injected by default. Use `manage_artifact(action=\"read\")` when exact content is needed."
        content = "<session_artifacts>\n" + "\n".join(lines) + suffix + "\n</session_artifacts>"
        return [synthetic_context_message(content, kind="artifact")]
