"""Compact CRUD facade for session artifacts."""
from typing import Any, Dict

from pycat.core.content.references import build_artifact_content_ref
from pycat.core.state.artifact import ArtifactService
from pycat.core.tools.base import BaseTool, ToolContext, ToolResult
from pycat.models.contracts.session_state import SessionState


class ManageArtifactTool(BaseTool):
    @property
    def name(self) -> str:
        return "state__artifact"

    @property
    def display_name(self) -> str:
        return "管理会话产物"

    @property
    def description(self) -> str:
        return "Store and retrieve artifacts by stable name; upsert replaces the body, while update preserves it."

    @property
    def category(self) -> str:
        return "state"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "read", "upsert", "append", "update", "delete"]},
                "name": {"type": "string", "description": "Stable artifact name; required except for list."},
                "content": {"type": "string", "description": "Required for upsert or append; omitted for update."},
                "kind": {"type": "string", "description": "Optional type such as plan, exploration, report, or note."},
                "status": {"type": "string", "description": "Optional lifecycle status such as draft, approved, or final."},
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        action = str(arguments.get("action") or "").strip().lower()
        name = ArtifactService.normalize_name(arguments.get("name"))
        content = str(arguments.get("content") or "")
        state = SessionState.from_dict(dict(context.state or {}))
        seq = int(context.state.get("_current_seq", 0) or 0)
        work_dir = str(context.work_dir or "").strip()
        conversation_id = getattr(getattr(context, "conversation", None), "id", None)

        if ArtifactService.reconcile_artifact_files(state, work_dir=work_dir, conversation_id=conversation_id, data_dir=context.data_dir):
            ArtifactService.sync_context_state(context.state, state)

        if action == "list":
            artifacts = ArtifactService.list_artifacts(state)
            if not artifacts:
                return ToolResult("No artifacts in this session.")
            lines = ["Session artifacts:"]
            for artifact_name, artifact in artifacts:
                preview = artifact.abstract[:160]
                lines.append(
                    f"- {artifact_name} kind={artifact.kind or '-'} status={artifact.status or '-'} "
                    f"chars={artifact.content_chars} abstract={preview or '-'}"
                )
            return ToolResult("\n".join(lines))

        if not name:
            return ToolResult("name is required for this action.", is_error=True)
        if action in {"upsert", "append"} and not content.strip():
            return ToolResult("content is required for upsert or append.", is_error=True)
        if action == "update":
            if "content" in arguments:
                return ToolResult("update preserves the body; use upsert to replace content.", is_error=True)
            if "kind" not in arguments and "status" not in arguments:
                return ToolResult("kind or status is required for update.", is_error=True)

        metadata = {
            "kind": arguments.get("kind") if "kind" in arguments else None,
            "status": arguments.get("status") if "status" in arguments else None,
        }
        if action == "upsert":
            artifact = ArtifactService.upsert_artifact(
                state,
                name=name,
                content=content,
                current_seq=seq,
                work_dir=work_dir,
                conversation_id=conversation_id,
                **metadata,
                data_dir=context.data_dir,
            )
            state.last_updated_seq = seq
            ArtifactService.sync_context_state(context.state, state)
            return self._mutation_result(
                f"Saved artifact '{name}' ({artifact.content_chars} chars).",
                artifact,
                work_dir=work_dir, conversation_id=conversation_id or "",
            )
        if action == "append":
            artifact = ArtifactService.append_artifact(
                state,
                name=name,
                content=content,
                current_seq=seq,
                work_dir=work_dir,
                conversation_id=conversation_id,
                **metadata,
                data_dir=context.data_dir,
            )
            state.last_updated_seq = seq
            ArtifactService.sync_context_state(context.state, state)
            return self._mutation_result(
                f"Appended artifact '{name}' ({artifact.content_chars} chars total).",
                artifact,
                work_dir=work_dir, conversation_id=conversation_id or "",
            )
        if action == "update":
            artifact = ArtifactService.update_artifact_metadata(
                state,
                name=name,
                current_seq=seq,
                work_dir=work_dir,
                conversation_id=conversation_id,
                **metadata,
                data_dir=context.data_dir,
            )
            if artifact is None:
                return ToolResult(f"Artifact '{name}' not found.", is_error=True)
            state.last_updated_seq = seq
            ArtifactService.sync_context_state(context.state, state)
            return self._mutation_result(
                f"Updated artifact '{name}' metadata; body preserved ({artifact.content_chars} chars).",
                artifact,
                work_dir=work_dir, conversation_id=conversation_id or "",
            )
        if action == "read":
            artifact = state.artifacts.get(name)
            if artifact is None:
                return ToolResult(f"Artifact '{name}' not found.", is_error=True)
            return ToolResult(ArtifactService.read_content_file(artifact, work_dir=work_dir, data_dir=context.data_dir))
        if action == "delete":
            if not ArtifactService.delete_artifact(state, name=name, work_dir=work_dir, data_dir=context.data_dir):
                return ToolResult(f"Artifact '{name}' not found.", is_error=True)
            state.last_updated_seq = seq
            ArtifactService.sync_context_state(context.state, state)
            return ToolResult(f"Deleted artifact '{name}'.", metadata={"changed_domains": ["artifact"]})
        return ToolResult(f"Unknown artifact action: {action}", is_error=True)

    @staticmethod
    def _mutation_result(message: str, artifact: Any, *, work_dir: str, conversation_id: str) -> ToolResult:
        metadata: dict[str, Any] = {"changed_domains": ["artifact"]}
        if str(getattr(artifact, "status", "") or "").strip().lower() == "final":
            metadata["content_refs"] = [build_artifact_content_ref(artifact, work_dir=work_dir, conversation_id=conversation_id).to_dict()]
        return ToolResult(message, metadata=metadata)
