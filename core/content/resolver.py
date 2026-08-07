"""Safe, read-only path resolution for conversation content references."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.content.archive_store import SessionArchiveStore
from core.state.artifact import ArtifactService
from models.contracts.content import ContentRef
from models.session_paths import resolve_session_root


class SessionContentResolver:
    """Route a navigation reference to its owning content service.

    This class only resolves existing local files.  It does not copy content,
    open a GUI application, or create a second content store.
    """

    def __init__(self, content_service: Any):
        self._content_service = content_service

    def resolve(self, conversation: Any, ref: ContentRef | dict[str, Any] | str) -> Path:
        normalized = self._coerce_ref(ref)
        kind = str(normalized.kind or "input").strip().lower()
        if kind == "input":
            return self._require_file(self._content_service.resolve_original(conversation, normalized))
        if kind == "workspace":
            return self._resolve_workspace(conversation, normalized)
        if kind == "artifact":
            return self._resolve_artifact(conversation, normalized)
        if kind == "archive":
            return self._resolve_archive(conversation, normalized)
        raise ValueError(f"unsupported content reference kind: {kind}")

    @staticmethod
    def _coerce_ref(ref: ContentRef | dict[str, Any] | str) -> ContentRef:
        if isinstance(ref, ContentRef):
            return ref
        if isinstance(ref, dict):
            return ContentRef.from_dict(ref)
        value = str(ref or "").strip()
        kind, _, identifier = value.partition(":")
        return ContentRef(
            id=identifier or value,
            name=identifier or value,
            mime="application/octet-stream",
            size=0,
            digest="",
            ref=value,
            kind=kind or "input",
        )

    @staticmethod
    def _require_file(path: Path) -> Path:
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(str(resolved))
        return resolved

    @staticmethod
    def _workspace_root(conversation: Any) -> Path:
        return Path(str(getattr(conversation, "work_dir", "") or ".")).expanduser().resolve()

    def _resolve_workspace(self, conversation: Any, ref: ContentRef) -> Path:
        if not str(getattr(conversation, "work_dir", "") or "").strip():
            raise ValueError("workspace references require an active workspace")
        root = self._workspace_root(conversation)
        raw = str(ref.ref or "").strip()
        if raw.startswith("workspace:"):
            raw = raw.split(":", 1)[1]
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        self._ensure_within(resolved, root)
        return self._require_file(resolved)

    def _resolve_artifact(self, conversation: Any, ref: ContentRef) -> Path:
        state = conversation.get_state()
        name = str(ref.id or "").strip()
        if not name and str(ref.ref or "").startswith("artifact:"):
            name = str(ref.ref).split(":", 1)[1].strip()
        artifact = state.artifacts.get(name)
        if artifact is None:
            normalized = ArtifactService.normalize_name(name)
            artifact = next(
                (
                    item
                    for item in (state.artifacts or {}).values()
                    if ArtifactService.normalize_name(getattr(item, "name", "")) == normalized
                ),
                None,
            )
        if artifact is None or not str(getattr(artifact, "content_path", "") or "").strip():
            raise FileNotFoundError(name or str(ref.ref or "artifact"))
        path = ArtifactService.resolve_content_path(
            str(artifact.content_path),
            work_dir=str(getattr(conversation, "work_dir", "") or "."),
        ).resolve()
        roots = [self._workspace_root(conversation)]
        if not str(getattr(conversation, "work_dir", "") or "").strip():
            roots.append(resolve_session_root("", str(getattr(conversation, "id", "") or "session")))
        self._ensure_within_any(path, roots)
        return self._require_file(path)

    def _resolve_archive(self, conversation: Any, ref: ContentRef) -> Path:
        content_id = str(ref.id or "").strip()
        if not content_id and str(ref.ref or "").startswith("archive:"):
            content_id = str(ref.ref).split(":", 1)[1].strip()
        store = SessionArchiveStore(
            str(getattr(conversation, "work_dir", "") or ""),
            conversation_id=getattr(conversation, "id", None),
        )
        record = store.read_record(content_id)
        if record is None:
            raise FileNotFoundError(content_id or str(ref.ref or "archive"))
        path = store.resolve_ref(record.original_ref)
        self._ensure_within(path.resolve(), store.session_root.resolve())
        return self._require_file(path)

    @staticmethod
    def _ensure_within(path: Path, root: Path) -> None:
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"content path escaped owner root: {path}") from exc

    @classmethod
    def _ensure_within_any(cls, path: Path, roots: list[Path]) -> None:
        for root in roots:
            try:
                path.relative_to(root.resolve())
                return
            except ValueError:
                continue
        raise ValueError(f"content path escaped owner roots: {path}")
