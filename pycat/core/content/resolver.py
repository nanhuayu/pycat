"""Safe, read-only path resolution for conversation content references."""

from __future__ import annotations

import mimetypes
import hashlib
from copy import copy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pycat.core.content.archive_store import SessionArchiveStore
from pycat.core.state.artifact import ArtifactService
from pycat.models.contracts.content import ContentRef
from pycat.models.conversation import Conversation
from pycat.models.session_paths import resolve_session_root
from pycat.models.session_paths import resolve_project_data_root
from pycat.models.workspace import WorkspaceLocation


@dataclass(frozen=True)
class ResolvedContent:
    """A verified path plus the metadata required by a content consumer."""

    path: Path
    ref: ContentRef

    @property
    def name(self) -> str:
        return str(self.ref.name or self.path.name or "content")

    @property
    def mime(self) -> str:
        return str(
            self.ref.mime
            or mimetypes.guess_type(self.name)[0]
            or "application/octet-stream"
        ).lower()

    @property
    def digest(self) -> str:
        return str(self.ref.digest or "")


class SessionContentResolver:
    """Route a navigation reference to its owning content service.

    Local references resolve to their owner files; SSH workspace references use
    verified disposable views from WorkspaceService. This owns no content store.
    """

    def __init__(self, content_service: Any):
        self._content_service = content_service
        self.data_dir = getattr(content_service, "data_dir", None)

    def resolve(self, conversation: Any, ref: ContentRef | dict[str, Any] | str) -> Path:
        return self.resolve_content(conversation, ref).path

    def resolve_content(
        self,
        conversation: Any,
        ref: ContentRef | dict[str, Any] | str,
        *, verify_digest: bool = True,
    ) -> ResolvedContent:
        """Resolve a reference without discarding its authoritative metadata."""
        normalized = self._coerce_ref(ref)
        if normalized.conversation_id and (normalized.conversation_id != getattr(conversation, "id", "")
                                            or normalized.workspace != str(getattr(conversation, "work_dir", "") or "")):
            conversation = Conversation(id=normalized.conversation_id, work_dir=normalized.workspace)
            conversation.data_dir = self.data_dir
        elif normalized.workspace:
            conversation = copy(conversation)
            conversation.work_dir = normalized.workspace
        kind = str(normalized.kind or "input").strip().lower()
        if kind == "input":
            loaded = self._content_service.load_ref(conversation, normalized)
            path = self._require_file(self._content_service.resolve_original(conversation, loaded))
            return ResolvedContent(path=path, ref=loaded)
        if kind == "workspace":
            path = self._resolve_workspace(conversation, normalized)
            if verify_digest:
                self._check_digest(normalized.digest, self._digest(path))
            return ResolvedContent(path=path, ref=self._workspace_ref(path, normalized))
        if kind == "artifact":
            path, artifact_ref = self._resolve_artifact_content(conversation, normalized, verify_digest=verify_digest)
            if verify_digest:
                self._check_digest(normalized.digest, artifact_ref.digest)
            return ResolvedContent(path=path, ref=artifact_ref)
        if kind == "archive":
            path, archive_ref = self._resolve_archive_content(conversation, normalized, verify_digest=verify_digest)
            if verify_digest:
                self._check_digest(normalized.digest, archive_ref.digest)
            return ResolvedContent(path=path, ref=archive_ref)
        if kind == "wiki":
            root = resolve_project_data_root(conversation.work_dir, data_dir=self.data_dir) / "wiki"
            path = (root / f"{normalized.id}.md").resolve()
            self._ensure_within(path, root.resolve())
            path = self._require_file(path)
            digest = self._digest(path) if verify_digest else normalized.digest
            if verify_digest:
                self._check_digest(normalized.digest, digest)
            return ResolvedContent(path, replace(normalized, digest=digest))
        raise ValueError(f"unsupported content reference kind: {kind}")

    @staticmethod
    def _digest(path: Path, *, text: bool = False) -> str:
        if not text:
            with path.open("rb") as stream:
                return hashlib.file_digest(stream, "sha256").hexdigest()
        digest = hashlib.sha256()
        with path.open("r", encoding="utf-8") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), ""):
                digest.update(chunk.encode("utf-8"))
        return digest.hexdigest()

    @staticmethod
    def _check_digest(expected: str, actual: str) -> None:
        if expected and expected != actual:
            raise ValueError("内容已变化，引用的历史版本不可用；请重新核实来源。")

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
        work_dir = str(getattr(conversation, "work_dir", "") or "").strip()
        if not work_dir:
            raise ValueError("workspace references require an active workspace")
        if WorkspaceLocation.parse(work_dir).is_remote:
            return resolve_project_data_root(work_dir, data_dir=getattr(conversation, "data_dir", None))
        return Path(work_dir).expanduser().resolve()

    def _resolve_workspace(self, conversation: Any, ref: ContentRef) -> Path:
        if not str(getattr(conversation, "work_dir", "") or "").strip():
            raise ValueError("workspace references require an active workspace")
        raw = str(ref.ref or "").strip()
        if raw.startswith("workspace:"):
            raw = raw.split(":", 1)[1]
        if WorkspaceLocation.parse(conversation.work_dir).is_remote:
            service = getattr(self._content_service, "workspace_service", None)
            if service is None:
                raise ValueError("SSH workspace service is unavailable")
            return service.materialize(conversation, raw, expected=ref.digest)
        root = self._workspace_root(conversation)
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        self._ensure_within(resolved, root)
        return self._require_file(resolved)

    @staticmethod
    def _workspace_ref(path: Path, requested: ContentRef) -> ContentRef:
        name = str(requested.name or path.name)
        mime = str(requested.mime or mimetypes.guess_type(name)[0] or "application/octet-stream")
        return replace(requested,
            id=str(requested.id or name),
            name=name,
            mime=mime,
            size=int(requested.size or 0),
            digest=str(requested.digest or ""),
            ref=str(requested.ref or f"workspace:{path.name}"),
            kind="workspace",
            source=str(requested.source or "agent"),
            status=str(requested.status or "ready"),
            message_id=str(requested.message_id or ""),
            created_at=str(requested.created_at or ""),
        )

    def _resolve_artifact(self, conversation: Any, ref: ContentRef) -> Path:
        path, _artifact_ref = self._resolve_artifact_content(conversation, ref)
        return path

    def _resolve_artifact_content(self, conversation: Any, ref: ContentRef, *, verify_digest: bool = True) -> tuple[Path, ContentRef]:
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
        if artifact is None and ref.conversation_id and ref.locator:
            path = ArtifactService.resolve_content_path(ref.locator, work_dir=ref.workspace, data_dir=getattr(conversation, "data_dir", None)).resolve()
            self._ensure_within(path, resolve_session_root(ref.workspace, ref.conversation_id, data_dir=getattr(conversation, "data_dir", None)).resolve())
            path = self._require_file(path)
            digest = self._digest(path, text=True) if verify_digest else ref.digest
            return path, replace(ref, digest=digest)
        if artifact is None or not str(getattr(artifact, "content_path", "") or "").strip():
            raise FileNotFoundError(name or str(ref.ref or "artifact"))
        path = ArtifactService.resolve_content_path(
            str(artifact.content_path),
            work_dir=str(getattr(conversation, "work_dir", "") or ""),
            data_dir=getattr(conversation, "data_dir", None),
        ).resolve()
        roots: list[Path] = []
        if str(getattr(conversation, "work_dir", "") or "").strip():
            roots.append(self._workspace_root(conversation))
        roots.append(resolve_session_root("", str(getattr(conversation, "id", "") or "session"), data_dir=getattr(conversation, "data_dir", None)))
        self._ensure_within_any(path, roots)
        path = self._require_file(path)
        digest = self._digest(path, text=True) if verify_digest else str(artifact.content_digest or "")
        return path, ContentRef(
            id=str(getattr(artifact, "name", "") or name),
            name=path.name,
            mime="text/markdown",
            size=int(getattr(artifact, "content_chars", 0) or 0),
            digest=digest,
            ref=f"artifact:{getattr(artifact, 'name', '') or name}",
            kind="artifact",
            source="artifact",
            status=str(getattr(artifact, "status", "ready") or "ready"),
            workspace=str(getattr(conversation, "work_dir", "") or ""),
            conversation_id=str(getattr(conversation, "id", "") or ""),
            locator=str(artifact.content_path),
        )

    def _resolve_archive(self, conversation: Any, ref: ContentRef) -> Path:
        path, _archive_ref = self._resolve_archive_content(conversation, ref)
        return path

    def _resolve_archive_content(self, conversation: Any, ref: ContentRef, *, verify_digest: bool = True) -> tuple[Path, ContentRef]:
        content_id = str(ref.id or "").strip()
        if not content_id and str(ref.ref or "").startswith("archive:"):
            content_id = str(ref.ref).split(":", 1)[1].strip()
        store = SessionArchiveStore(
            str(getattr(conversation, "work_dir", "") or ""),
            conversation_id=getattr(conversation, "id", None),
            data_dir=getattr(conversation, "data_dir", None),
        )
        record_id, separator, image_index = content_id.partition("/images/")
        if separator and (not image_index.isdecimal() or not 1 <= int(image_index) <= 16):
            raise ValueError("invalid archive image reference")
        record = store.read_record(record_id)
        if record is None:
            raise FileNotFoundError(content_id or str(ref.ref or "archive"))
        if separator:
            return store.resolve_image(record, int(image_index), verify_digest=verify_digest)
        if verify_digest and not store.original_matches(record, store.read_original(record)):
            raise ValueError("archive original changed; the pinned version is unavailable")
        path = store.resolve_ref(record.original_ref)
        self._ensure_within(path.resolve(), store.session_root.resolve())
        path = self._require_file(path)
        return path, ContentRef(
            id=str(record.id or content_id),
            name=path.name,
            mime="text/plain",
            size=int(record.size or 0),
            digest=str(record.digest or ""),
            ref=f"archive:{record.id}",
            kind="archive",
            source=str(record.source or "archive"),
            status=str(record.status or "ready"),
            created_at=str(record.created_at or ""),
            workspace=str(getattr(conversation, "work_dir", "") or ""),
            conversation_id=str(getattr(conversation, "id", "") or ""),
            locator=ref.locator,
        )

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
