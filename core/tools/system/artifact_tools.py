"""CRUD for session artifacts stored inside SessionState."""

from typing import Any, Dict

from core.state.services.artifact_service import ArtifactService
from core.tools.base import BaseTool, ToolContext, ToolResult
from models.state import SessionState


class ManageArtifactTool(BaseTool):
    """Create, read, update, or delete model-managed session artifacts."""

    @property
    def name(self) -> str:
        return "manage_artifact"

    @property
    def description(self) -> str:
        return (
            "Manage session artifacts such as plans, reports, notes, and references. "
            "Artifacts are session outputs or working material, not memory facts and not project instructions. "
            "The prompt sees artifact indexes/abstracts by default; use read for full content when needed. "
            "Artifact Markdown files are written with frontmatter metadata when available.\n\n"
            "When to use:\n"
            "- Create/update `plan` for non-trivial execution plans and design checkpoints.\n"
            "- Create/update `exploration` for reusable multi-source or multi-file findings.\n"
            "- Create/update `report` before completion when the user requested a report, timeline, document, or substantial summary.\n"
            "- If an artifact index already exists for the same topic, read it first and update/append instead of duplicating.\n\n"
            "Actions:\n"
            "- create/update: Set or replace an artifact's content\n"
            "- read: Read an artifact's full content\n"
            "- delete: Remove an artifact\n"
            "- list: List artifact indexes\n"
            "- append: Append text to an existing artifact"
        )

    @property
    def category(self) -> str:
        return "manage"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "read", "update", "delete", "list", "append"],
                    "description": "The action to perform",
                },
                "name": {
                    "type": "string",
                    "description": "Artifact name, e.g. plan, report, notes. Required except for list.",
                },
                "content": {
                    "type": "string",
                    "description": "Artifact content. Required for create/update/append.",
                },
                "abstract": {
                    "type": "string",
                    "description": "Short abstract for indexing and prompt previews.",
                },
                "kind": {
                    "type": "string",
                    "description": "Optional artifact kind, e.g. plan, note, report, reference.",
                },
                "status": {
                    "type": "string",
                    "description": "Artifact lifecycle status, e.g. draft, approved, final, archived.",
                },
                "references": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Important file paths, URLs, or code locations related to the artifact.",
                },
                "related": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Related artifact names, file paths, URLs, or code locations.",
                },
                "frontmatter": {
                    "type": "object",
                    "description": "Optional structured metadata that mirrors Markdown frontmatter.",
                },
            },
            "required": ["action"],
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        action = arguments.get("action", "")
        name = ArtifactService.normalize_name(arguments.get("name"))
        content = arguments.get("content", "")
        abstract = arguments.get("abstract") if "abstract" in arguments else None
        kind = arguments.get("kind") if "kind" in arguments else None
        status = arguments.get("status") if "status" in arguments else None
        references = arguments.get("references") if "references" in arguments else None
        related = arguments.get("related") if "related" in arguments else None
        frontmatter = arguments.get("frontmatter") if "frontmatter" in arguments else None

        state = SessionState.from_dict(dict(context.state or {}))
        seq = int((context.state or {}).get("_current_seq", 0))
        conversation_id = getattr(getattr(context, "conversation", None), "id", None)
        work_dir = str(getattr(context, "work_dir", ".") or ".")

        if action == "list":
            artifacts = ArtifactService.list_artifacts(state)
            if not artifacts:
                return ToolResult("No artifacts in this session.")
            lines = []
            for artifact_name, artifact in artifacts:
                preview_source = artifact.abstract
                preview = (preview_source[:80] + "...") if len(preview_source) > 80 else preview_source
                label = f" [{artifact.kind}]" if artifact.kind else ""
                status_label = f" status={artifact.status}" if artifact.status else ""
                related_label = f" related={', '.join(artifact.related[:3])}" if artifact.related else ""
                path_label = f" path={artifact.content_path}" if artifact.content_path else ""
                lines.append(f"- **{artifact_name}**{label}{status_label}{related_label}{path_label} ({artifact.content_chars} chars): {preview}")
            return ToolResult("\n".join(lines))

        if not name:
            return ToolResult("'name' is required for this action.", is_error=True)

        if action in ("create", "update"):
            ArtifactService.upsert_artifact(
                state,
                name=name,
                content=content,
                current_seq=seq,
                abstract=abstract,
                kind=kind,
                status=status,
                references=references,
                related=related,
                frontmatter=frontmatter,
                work_dir=work_dir,
                conversation_id=conversation_id,
            )
            state.last_updated_seq = seq
            ArtifactService.sync_context_state(context.state, state)
            return ToolResult(f"Artifact '{name}' saved ({len(content)} chars).")

        if action == "append":
            ArtifactService.append_artifact(
                state,
                name=name,
                content=content,
                current_seq=seq,
                abstract=abstract,
                kind=kind,
                status=status,
                references=references,
                related=related,
                frontmatter=frontmatter,
                work_dir=work_dir,
                conversation_id=conversation_id,
            )
            state.last_updated_seq = seq
            ArtifactService.sync_context_state(context.state, state)
            total = state.artifacts[name].content_chars
            return ToolResult(f"Appended to artifact '{name}' (total {total} chars).")

        if action == "read":
            artifact = state.artifacts.get(name)
            if artifact is None:
                return ToolResult(f"Artifact '{name}' not found.", is_error=True)
            return ToolResult(ArtifactService.read_content_file(artifact, work_dir=work_dir))

        if action == "delete":
            if not ArtifactService.delete_artifact(state, name=name, work_dir=work_dir):
                return ToolResult(f"Artifact '{name}' not found.", is_error=True)
            state.last_updated_seq = seq
            ArtifactService.sync_context_state(context.state, state)
            return ToolResult(f"Artifact '{name}' deleted.")

        return ToolResult(f"Unknown action: {action}", is_error=True)