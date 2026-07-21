from __future__ import annotations

from core.content.markdown import trim_text
from core.context.providers.base import MessageProviderMixin, ProviderContext, context_item


class ArtifactProvider(MessageProviderMixin):
    name = "artifact"
    priority = 35

    def build_items(self, context: ProviderContext):
        try:
            artifacts = context.conversation.get_state().artifacts or {}
        except Exception:
            artifacts = {}
        if not artifacts:
            return []
        lines = ["<session_artifacts>"]
        for name, artifact in sorted(artifacts.items()):
            abstract = trim_text(str(getattr(artifact, "abstract", "") or ""), 300) or "-"
            kind = str(getattr(artifact, "kind", "") or "-")
            status = str(getattr(artifact, "status", "") or "-")
            chars = int(getattr(artifact, "content_chars", 0) or 0)
            digest = str(getattr(artifact, "content_digest", "") or "")[:16]
            lines.append(
                f"- {name} kind={kind} status={status} chars={chars} digest={digest or '-'} abstract={abstract}"
            )
        lines.append("</session_artifacts>")
        return [
            context_item(
                "\n".join(lines),
                kind=self.name,
                priority=self.priority,
                item_id="artifact:index",
                source_ref="SessionState.artifacts",
            )
        ]
