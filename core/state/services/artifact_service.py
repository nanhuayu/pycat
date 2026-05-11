import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from core.content.markdown import strip_frontmatter, with_frontmatter
from models.state import SessionArtifact, SessionState


class ArtifactService:
    @staticmethod
    def normalize_name(name: object) -> str:
        return str(name or "").strip().lower()

    @staticmethod
    def artifact_storage_dir(*, work_dir: str, conversation_id: object = None) -> Path:
        session_id = str(conversation_id or "session").strip() or "session"
        return Path(work_dir or ".").expanduser().resolve() / ".pycat" / "sessions" / session_id / "artifacts"

    @staticmethod
    def artifact_file_path(*, work_dir: str, conversation_id: object = None, name: str) -> Path:
        normalized = ArtifactService.normalize_name(name)
        safe = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "-", normalized).strip(" .-")
        safe = safe[:80].strip(" .-") or "artifact"
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:10]
        return ArtifactService.artifact_storage_dir(work_dir=work_dir, conversation_id=conversation_id) / f"{safe}-{digest}.md"

    @staticmethod
    def relative_content_path(path: Path, *, work_dir: str) -> str:
        root = Path(work_dir or ".").expanduser().resolve()
        try:
            return path.resolve().relative_to(root).as_posix()
        except Exception:
            return path.as_posix()

    @staticmethod
    def resolve_content_path(content_path: str, *, work_dir: str) -> Path:
        path = Path(str(content_path or "").strip())
        if not path.is_absolute():
            path = Path(work_dir or ".").expanduser().resolve() / path
        return path

    @staticmethod
    def write_content_file(
        artifact: SessionArtifact,
        *,
        content: str,
        work_dir: str,
        conversation_id: object = None,
    ) -> None:
        target = ArtifactService.artifact_file_path(
            work_dir=work_dir,
            conversation_id=conversation_id,
            name=artifact.name,
        )
        text = with_frontmatter(str(content or ""), ArtifactService.artifact_frontmatter(artifact))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        artifact.content = ""
        artifact.content_path = ArtifactService.relative_content_path(target, work_dir=work_dir)
        artifact.content_chars = len(text)
        artifact.content_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def read_content_file(artifact: SessionArtifact, *, work_dir: str) -> str:
        if artifact.content_path:
            path = ArtifactService.resolve_content_path(artifact.content_path, work_dir=work_dir)
            if path.exists() and path.is_file():
                return path.read_text(encoding="utf-8")
        return str(artifact.content or "")

    @staticmethod
    def delete_content_file(artifact: SessionArtifact, *, work_dir: str) -> None:
        if not artifact.content_path:
            return
        try:
            ArtifactService.resolve_content_path(artifact.content_path, work_dir=work_dir).unlink(missing_ok=True)
        except Exception:
            return

    @staticmethod
    def default_abstract(content: str) -> str:
        text = strip_frontmatter(str(content or "")).strip()
        if not text:
            return ""
        first = next((line.strip(" #\t") for line in text.splitlines() if line.strip()), "")
        return first[:240]

    @staticmethod
    def artifact_frontmatter(artifact: SessionArtifact) -> Dict[str, Any]:
        metadata = ArtifactService.normalize_frontmatter(getattr(artifact, "frontmatter", {}) or {})
        if artifact.name:
            metadata.setdefault("name", artifact.name)
        if artifact.kind:
            metadata.setdefault("kind", artifact.kind)
        if artifact.status:
            metadata.setdefault("status", artifact.status)
        if artifact.references:
            metadata.setdefault("references", list(artifact.references))
        if artifact.related:
            metadata.setdefault("related", list(artifact.related))
        if artifact.updated_seq:
            metadata.setdefault("updated_seq", int(artifact.updated_seq))
        return metadata

    @staticmethod
    def list_artifacts(state: SessionState) -> List[Tuple[str, SessionArtifact]]:
        return [(name, artifact) for name, artifact in state.artifacts.items() if str(name or "").strip()]

    @staticmethod
    def normalize_references(references: object) -> List[str]:
        if not isinstance(references, list):
            return []
        out: List[str] = []
        for item in references:
            value = str(item or "").strip()
            if value and value not in out:
                out.append(value)
        return out

    @staticmethod
    def normalize_status(status: object, *, default: str = "draft") -> str:
        value = str(status or "").strip().lower()
        return value or default

    @staticmethod
    def normalize_frontmatter(frontmatter: object) -> Dict[str, Any]:
        if not isinstance(frontmatter, dict):
            return {}
        normalized: Dict[str, Any] = {}
        for key, value in frontmatter.items():
            norm_key = str(key or "").strip().lower()
            if not norm_key:
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                normalized[norm_key] = value
            elif isinstance(value, list):
                normalized[norm_key] = [str(item) for item in value if str(item).strip()]
            else:
                normalized[norm_key] = str(value)
        return normalized

    @staticmethod
    def upsert_artifact(
        state: SessionState,
        *,
        name: str,
        content: str,
        current_seq: int,
        abstract: object = None,
        kind: object = None,
        status: object = None,
        references: object = None,
        related: object = None,
        frontmatter: object = None,
        work_dir: str = ".",
        conversation_id: object = None,
    ) -> SessionArtifact:
        normalized = ArtifactService.normalize_name(name)
        artifact = state.ensure_artifact(normalized)
        artifact.name = normalized
        if abstract is not None:
            artifact.abstract = str(abstract or "").strip()
        elif not artifact.abstract:
            artifact.abstract = ArtifactService.default_abstract(str(content or ""))
        if kind is not None:
            artifact.kind = str(kind or "").strip().lower()
        if status is not None:
            artifact.status = ArtifactService.normalize_status(status)
        if references is not None:
            artifact.references = ArtifactService.normalize_references(references)
        if related is not None:
            artifact.related = ArtifactService.normalize_references(related)
        if frontmatter is not None:
            artifact.frontmatter = ArtifactService.normalize_frontmatter(frontmatter)
        artifact.updated_seq = int(current_seq)
        ArtifactService.write_content_file(
            artifact,
            content=str(content or ""),
            work_dir=work_dir,
            conversation_id=conversation_id,
        )
        return artifact

    @staticmethod
    def append_artifact(
        state: SessionState,
        *,
        name: str,
        content: str,
        current_seq: int,
        abstract: object = None,
        kind: object = None,
        status: object = None,
        references: object = None,
        related: object = None,
        frontmatter: object = None,
        work_dir: str = ".",
        conversation_id: object = None,
    ) -> SessionArtifact:
        normalized = ArtifactService.normalize_name(name)
        artifact = state.ensure_artifact(normalized)
        artifact.name = normalized
        addition = str(content or "")
        existing = strip_frontmatter(ArtifactService.read_content_file(artifact, work_dir=work_dir))
        if existing and addition:
            next_content = existing + "\n" + addition
        elif addition:
            next_content = addition
        else:
            next_content = existing
        if abstract is not None:
            artifact.abstract = str(abstract or "").strip()
        elif not artifact.abstract:
            artifact.abstract = ArtifactService.default_abstract(next_content)
        if kind is not None:
            artifact.kind = str(kind or "").strip().lower()
        if status is not None:
            artifact.status = ArtifactService.normalize_status(status)
        if references is not None:
            artifact.references = ArtifactService.normalize_references(references)
        if related is not None:
            artifact.related = ArtifactService.normalize_references(related)
        if frontmatter is not None:
            artifact.frontmatter = ArtifactService.normalize_frontmatter(frontmatter)
        artifact.updated_seq = int(current_seq)
        ArtifactService.write_content_file(
            artifact,
            content=next_content,
            work_dir=work_dir,
            conversation_id=conversation_id,
        )
        return artifact

    @staticmethod
    def delete_artifact(state: SessionState, *, name: str, work_dir: str = ".") -> bool:
        normalized = ArtifactService.normalize_name(name)
        if normalized not in state.artifacts:
            return False
        artifact = state.artifacts.pop(normalized)
        ArtifactService.delete_content_file(artifact, work_dir=work_dir)
        return True

    @staticmethod
    def sync_context_state(context_state: Dict[str, object], state: SessionState) -> None:
        context_state.clear()
        context_state.update(state.to_dict())