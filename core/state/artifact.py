import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from core.content.markdown import parse_frontmatter, strip_frontmatter, with_frontmatter
from core.state.operations import ensure_artifact
from models.contracts.session_state import SessionArtifact, SessionState
from models.session_paths import resolve_session_root


_MANAGED_FRONTMATTER_KEYS = {
    "name",
    "kind",
    "status",
    "references",
    "related",
    "updated_seq",
}


class ArtifactService:
    @staticmethod
    def normalize_name(name: object) -> str:
        return str(name or "").strip().lower()

    @staticmethod
    def artifact_storage_dir(*, work_dir: str, conversation_id: object = None) -> Path:
        session_id = str(conversation_id or "session").strip() or "session"
        raw = str(work_dir or "").strip()
        root = Path(raw).expanduser().resolve() if raw else ""
        return resolve_session_root(root, session_id) / "artifact"

    @staticmethod
    def artifact_file_path(*, work_dir: str, conversation_id: object = None, name: str) -> Path:
        safe = ArtifactService._safe_artifact_stem(name)
        return ArtifactService.artifact_storage_dir(work_dir=work_dir, conversation_id=conversation_id) / f"{safe}.md"

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
        artifact.frontmatter = ArtifactService.custom_frontmatter(artifact.frontmatter)
        text = with_frontmatter(str(content or ""), ArtifactService.artifact_frontmatter(artifact))
        target = ArtifactService._content_write_path(
            artifact,
            content=text,
            work_dir=work_dir,
            conversation_id=conversation_id,
        )
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
    def reconcile_artifact_files(
        state: SessionState,
        *,
        work_dir: str,
        conversation_id: object = None,
    ) -> int:
        """Rebuild missing artifact index entries from durable Markdown files.

        Artifact Markdown is the recoverable content layer; ``SessionState`` is
        only the lightweight index. If a runtime/UI sync failure leaves a file
        on disk without a state entry, this restores the index without copying
        the full body into state.
        """
        artifact_dir = ArtifactService.artifact_storage_dir(
            work_dir=work_dir,
            conversation_id=conversation_id,
        )
        if not artifact_dir.exists() or not artifact_dir.is_dir():
            return 0

        changed = 0
        for path in sorted(artifact_dir.glob("*.md"), key=lambda item: item.name.lower()):
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                continue
            metadata, body = parse_frontmatter(text)
            name = ArtifactService._name_from_artifact_file(path, metadata)
            if not name:
                continue
            normalized = ArtifactService.normalize_name(name)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            rel_path = ArtifactService.relative_content_path(path, work_dir=work_dir)
            existing = state.artifacts.get(normalized)
            existing_path_ok = False
            existing_path_matches = False
            if existing is not None and existing.content_path:
                try:
                    existing_path = ArtifactService.resolve_content_path(existing.content_path, work_dir=work_dir)
                    existing_path_ok = existing_path.exists() and existing_path.is_file()
                    existing_path_matches = existing_path.resolve() == path.resolve()
                except Exception:
                    existing_path_ok = False
                    existing_path_matches = False
            recovered_body = ArtifactService._recover_imported_body(
                artifact_path=path,
                metadata=metadata,
                body=body,
                work_dir=work_dir,
            )
            index_stale = bool(
                existing is not None
                and existing_path_matches
                and (
                    str(existing.content_digest or "") != digest
                    or int(existing.content_chars or 0) != len(text)
                )
            )
            frontmatter_stale = bool(
                existing is not None
                and existing_path_matches
                and (
                    not ArtifactService._managed_frontmatter_is_current(existing, metadata)
                    or any(key in (existing.frontmatter or {}) for key in _MANAGED_FRONTMATTER_KEYS)
                )
            )
            if existing is not None and existing_path_ok and not (
                existing_path_matches and (recovered_body or index_stale or frontmatter_stale)
            ):
                continue

            artifact = existing or SessionArtifact(name=normalized)
            artifact.name = normalized
            artifact.content = ""
            artifact.abstract = (
                artifact.abstract
                or str(metadata.get("abstract") or "").strip()
                or ArtifactService.default_abstract(recovered_body or text)
            )
            artifact.kind = artifact.kind or str(metadata.get("kind") or "").strip().lower()
            artifact.status = ArtifactService.normalize_status(
                (artifact.status or metadata.get("status"))
                if existing is not None
                else metadata.get("status"),
                default="draft",
            )
            if not artifact.references:
                artifact.references = ArtifactService.normalize_references(metadata.get("references"))
            if not artifact.related:
                artifact.related = ArtifactService.normalize_references(metadata.get("related"))
            if not artifact.frontmatter:
                artifact.frontmatter = ArtifactService.custom_frontmatter(metadata)
            artifact.content_path = rel_path
            try:
                artifact.updated_seq = int(artifact.updated_seq or metadata.get("updated_seq") or 0)
            except Exception:
                artifact.updated_seq = int(artifact.updated_seq or 0)
            if recovered_body or frontmatter_stale:
                ArtifactService.write_content_file(
                    artifact,
                    content=recovered_body or body,
                    work_dir=work_dir,
                    conversation_id=conversation_id,
                )
            else:
                artifact.content_digest = digest
                artifact.content_chars = len(text)
            state.artifacts[normalized] = artifact
            changed += 1

        if changed:
            state.state_version += changed
            updated = [
                int(getattr(artifact, "updated_seq", 0) or 0)
                for artifact in state.artifacts.values()
            ]
            if updated:
                state.last_updated_seq = max(int(state.last_updated_seq or 0), max(updated))
        return changed

    @staticmethod
    def _name_from_artifact_file(path: Path, metadata: Dict[str, Any]) -> str:
        stem_name = re.sub(r"-[0-9a-f]{8,10}$", "", path.stem, flags=re.IGNORECASE).strip()
        if stem_name:
            return stem_name
        raw_name = metadata.get("name") if isinstance(metadata, dict) else ""
        return str(raw_name or "").strip()

    @staticmethod
    def import_artifact_file(
        state: SessionState,
        *,
        source_path: str | Path,
        work_dir: str,
        conversation_id: object = None,
        current_seq: int = 0,
        provenance: Dict[str, Any] | None = None,
    ) -> Tuple[SessionArtifact, bool]:
        """Import an external artifact into the current session workspace.

        The current session owns the workbench artifact. Child sessions remain
        provenance only. Re-importing the same source updates refs/metadata
        without appending duplicate content; new sources with the same artifact
        name are merged as bounded imported-source blocks.
        """
        source = ArtifactService.resolve_content_path(str(source_path or ""), work_dir=work_dir)
        text = source.read_text(encoding="utf-8")
        source_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        metadata, body = parse_frontmatter(text)
        provenance_payload = dict(provenance or {})
        source_session_id = str(provenance_payload.get("source_session_id") or metadata.get("source_session_id") or "").strip()
        source_artifact_path = str(provenance_payload.get("source_artifact_path") or source_path or "").strip()
        source_key = ArtifactService._source_key(
            source_session_id=source_session_id,
            source_artifact_path=source_artifact_path,
            source_digest=source_digest,
        )

        target_name = ArtifactService._import_target_name(
            provenance=provenance_payload,
            metadata=metadata,
            source=source,
            body=body,
        )
        existing = state.artifacts.get(target_name)
        imported = existing or SessionArtifact(name=target_name)
        imported.name = target_name
        imported.kind = str(provenance_payload.get("kind") or metadata.get("kind") or imported.kind or "artifact").strip().lower()
        imported.status = ArtifactService.normalize_status(
            provenance_payload.get("status") or metadata.get("status") or imported.status or "draft",
            default=imported.status or "draft",
        )
        imported.abstract = (
            str(provenance_payload.get("abstract") or "").strip()
            or imported.abstract
            or str(metadata.get("abstract") or "").strip()
            or ArtifactService.default_abstract(body)
        )
        imported.references = ArtifactService._merge_unique(
            imported.references,
            ArtifactService.normalize_references(metadata.get("references")),
            ArtifactService.normalize_references(provenance_payload.get("references")),
        )
        imported.related = ArtifactService._merge_unique(
            imported.related,
            ArtifactService.normalize_references(metadata.get("related")),
            ArtifactService.normalize_references(provenance_payload.get("related")),
            [
                source_session_id,
                str(provenance_payload.get("agent_run_id") or "").strip(),
            ],
        )
        frontmatter = ArtifactService.custom_frontmatter(imported.frontmatter)
        if not frontmatter:
            frontmatter = ArtifactService.custom_frontmatter(metadata)
        existing_source_keys = ArtifactService._frontmatter_list(frontmatter.get("source_keys"))
        already_imported = bool(source_key and source_key in existing_source_keys)
        if source_key and not already_imported:
            frontmatter["source_keys"] = ArtifactService._merge_unique(existing_source_keys, [source_key])
        elif existing_source_keys:
            frontmatter["source_keys"] = existing_source_keys

        for key, value in {
            "source_session_id": source_session_id,
            "source_artifact_path": source_artifact_path,
            "source_digest": source_digest,
            "agent_run_id": provenance_payload.get("agent_run_id"),
            "imported_from": provenance_payload.get("imported_from") or "agent__run",
        }.items():
            if value not in (None, "", []):
                frontmatter[str(key)] = value
        imported.frontmatter = frontmatter
        imported.updated_seq = int(current_seq or provenance_payload.get("updated_seq") or imported.updated_seq or 0)
        state.artifacts[target_name] = imported

        if already_imported and existing is not None:
            current_body = strip_frontmatter(
                ArtifactService.read_content_file(imported, work_dir=work_dir)
            )
            if not current_body.strip() and body.strip():
                current_body = body
            ArtifactService.write_content_file(
                imported,
                content=current_body,
                work_dir=work_dir,
                conversation_id=conversation_id,
            )
            state.last_updated_seq = max(int(state.last_updated_seq or 0), int(imported.updated_seq or 0))
            state.state_version += 1
            return imported, False

        if existing is None:
            next_content = body
        else:
            current_body = strip_frontmatter(ArtifactService.read_content_file(imported, work_dir=work_dir))
            next_content = ArtifactService._merge_imported_source(
                existing_body=current_body,
                imported_body=body,
                kind=imported.kind,
                title=ArtifactService._source_title(metadata=metadata, source=source, body=body, provenance=provenance_payload),
                source_session_id=source_session_id,
                source_artifact_path=source_artifact_path,
                source_digest=source_digest,
            )

        ArtifactService.write_content_file(
            imported,
            content=next_content,
            work_dir=work_dir,
            conversation_id=conversation_id,
        )
        state.last_updated_seq = max(int(state.last_updated_seq or 0), int(imported.updated_seq or 0))
        state.state_version += 1
        return imported, True

    @staticmethod
    def _content_write_path(
        artifact: SessionArtifact,
        *,
        content: str,
        work_dir: str,
        conversation_id: object = None,
    ) -> Path:
        if artifact.content_path:
            return ArtifactService.resolve_content_path(artifact.content_path, work_dir=work_dir)

        target = ArtifactService.artifact_file_path(
            work_dir=work_dir,
            conversation_id=conversation_id,
            name=artifact.name,
        )
        if not target.exists():
            return target

        try:
            metadata, _body = parse_frontmatter(target.read_text(encoding="utf-8"))
            if ArtifactService.normalize_name(metadata.get("name")) == ArtifactService.normalize_name(artifact.name):
                return target
        except Exception:
            pass

        safe = ArtifactService._safe_artifact_stem(artifact.name)
        digest = hashlib.sha1(ArtifactService.normalize_name(artifact.name).encode("utf-8")).hexdigest()[:8]
        candidate = target.with_name(f"{safe}-{digest}.md")
        if not candidate.exists():
            return candidate

        content_digest = hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()[:8]
        return target.with_name(f"{safe}-{digest}-{content_digest}.md")

    @staticmethod
    def _safe_artifact_stem(name: object) -> str:
        normalized = ArtifactService.normalize_name(name)
        safe = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "-", normalized).strip(" .-")
        return safe[:80].strip(" .-") or "artifact"

    @staticmethod
    def _import_target_name(
        *,
        provenance: Dict[str, Any],
        metadata: Dict[str, Any],
        source: Path,
        body: str,
    ) -> str:
        candidates = [
            provenance.get("name"),
            provenance.get("id"),
            metadata.get("name"),
            ArtifactService._markdown_h1(body),
            provenance.get("title"),
            provenance.get("goal"),
            ArtifactService._name_from_artifact_file(source, metadata),
        ]
        for candidate in candidates:
            normalized = ArtifactService.normalize_name(candidate)
            if normalized and not ArtifactService._is_generic_artifact_name(normalized):
                return normalized
        kind = str(provenance.get("kind") or metadata.get("kind") or "").strip().lower()
        return ArtifactService.normalize_name(ArtifactService._markdown_h1(body) or kind or "artifact")

    @staticmethod
    def _is_generic_artifact_name(name: str) -> bool:
        return ArtifactService.normalize_name(name) in {
            "artifact",
            "document",
            "exploration",
            "findings",
            "notes",
            "plan",
            "report",
            "summary",
        }

    @staticmethod
    def _merge_unique(*groups: object) -> List[str]:
        merged: List[str] = []
        for group in groups:
            values = group if isinstance(group, list) else [group]
            for item in values:
                value = str(item or "").strip()
                if value and value not in merged:
                    merged.append(value)
        return merged

    @staticmethod
    def _frontmatter_list(value: object) -> List[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value or "").strip()
        return [text] if text else []

    @staticmethod
    def _source_key(*, source_session_id: str, source_artifact_path: str, source_digest: str) -> str:
        parts = [
            str(source_session_id or "").strip(),
            str(source_artifact_path or "").strip(),
            str(source_digest or "").strip(),
        ]
        return "#".join(part for part in parts if part)

    @staticmethod
    def _recover_imported_body(
        *,
        artifact_path: Path,
        metadata: Dict[str, Any],
        body: str,
        work_dir: str,
    ) -> str:
        if str(body or "").strip():
            return ""
        source_path = str(metadata.get("source_artifact_path") or "").strip()
        expected_digest = str(metadata.get("source_digest") or "").strip().lower()
        if not source_path or not expected_digest:
            return ""
        try:
            source = ArtifactService.resolve_content_path(source_path, work_dir=work_dir)
            if source.resolve() == artifact_path.resolve() or not source.is_file():
                return ""
            source_text = source.read_text(encoding="utf-8")
        except Exception:
            return ""
        source_digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        if source_digest.lower() != expected_digest:
            return ""
        _source_metadata, source_body = parse_frontmatter(source_text)
        return source_body if source_body.strip() else ""

    @staticmethod
    def _managed_frontmatter_is_current(
        artifact: SessionArtifact,
        metadata: Dict[str, Any],
    ) -> bool:
        expected = ArtifactService.artifact_frontmatter(artifact)
        scalar_keys = ("name", "kind", "status")
        for key in scalar_keys:
            if str(metadata.get(key) or "").strip() != str(expected.get(key) or "").strip():
                return False
        for key in ("references", "related"):
            if ArtifactService.normalize_references(metadata.get(key)) != ArtifactService.normalize_references(
                expected.get(key)
            ):
                return False
        try:
            actual_seq = int(metadata.get("updated_seq") or 0)
        except Exception:
            actual_seq = 0
        return actual_seq == int(expected.get("updated_seq") or 0)

    @staticmethod
    def _markdown_h1(body: str) -> str:
        for line in str(body or "").splitlines():
            text = line.strip()
            if text.startswith("# "):
                return text.lstrip("#").strip()
        return ""

    @staticmethod
    def _source_title(
        *,
        metadata: Dict[str, Any],
        source: Path,
        body: str,
        provenance: Dict[str, Any],
    ) -> str:
        return (
            str(provenance.get("title") or "").strip()
            or str(metadata.get("title") or metadata.get("name") or "").strip()
            or ArtifactService._markdown_h1(body)
            or source.stem
        )

    @staticmethod
    def _merge_imported_source(
        *,
        existing_body: str,
        imported_body: str,
        kind: str,
        title: str,
        source_session_id: str,
        source_artifact_path: str,
        source_digest: str,
    ) -> str:
        source_block = ArtifactService._imported_source_block(
            body=imported_body,
            title=title,
            source_session_id=source_session_id,
            source_artifact_path=source_artifact_path,
            source_digest=source_digest,
        )
        base = str(existing_body or "").rstrip()
        if str(kind or "").strip().lower() == "report":
            note = (
                "\n\n## Conflicts / Needs Synthesis\n\n"
                f"- Imported source `{title}` was added as source notes. Review it against the current report before final synthesis."
            )
            if "## Conflicts / Needs Synthesis" in base:
                note = (
                    f"\n- Imported source `{title}` was added as source notes. Review it against the current report before final synthesis."
                )
            return (base + note + "\n\n" + source_block).strip() if base else source_block
        return (base + "\n\n" + source_block).strip() if base else source_block

    @staticmethod
    def _imported_source_block(
        *,
        body: str,
        title: str,
        source_session_id: str,
        source_artifact_path: str,
        source_digest: str,
    ) -> str:
        lines = [
            f"## Imported Source: {str(title or 'source').strip()}",
            "",
        ]
        if source_session_id:
            lines.append(f"- source_session_id: `{source_session_id}`")
        if source_artifact_path:
            lines.append(f"- source_artifact_path: `{source_artifact_path}`")
        if source_digest:
            lines.append(f"- source_digest: `{source_digest}`")
        lines.extend(["", str(body or "").strip()])
        return "\n".join(lines).strip()

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
        metadata = ArtifactService.custom_frontmatter(getattr(artifact, "frontmatter", {}) or {})
        managed = {
            "name": artifact.name,
            "kind": artifact.kind,
            "status": artifact.status,
            "references": list(artifact.references),
            "related": list(artifact.related),
            "updated_seq": int(artifact.updated_seq or 0),
        }
        for key, value in managed.items():
            if value not in (None, "", [], 0):
                metadata[key] = value
            else:
                metadata.pop(key, None)
        return metadata

    @staticmethod
    def custom_frontmatter(frontmatter: object) -> Dict[str, Any]:
        metadata = ArtifactService.normalize_frontmatter(frontmatter)
        for key in _MANAGED_FRONTMATTER_KEYS:
            metadata.pop(key, None)
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
        artifact = ensure_artifact(state, normalized)
        artifact.name = normalized
        if abstract is not None:
            artifact.abstract = str(abstract or "").strip()
        else:
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
        artifact = ensure_artifact(state, normalized)
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
        else:
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
    def update_artifact_metadata(
        state: SessionState,
        *,
        name: str,
        current_seq: int,
        kind: object = None,
        status: object = None,
        work_dir: str = ".",
        conversation_id: object = None,
    ) -> SessionArtifact | None:
        normalized = ArtifactService.normalize_name(name)
        artifact = state.artifacts.get(normalized)
        if artifact is None:
            return None
        body = strip_frontmatter(
            ArtifactService.read_content_file(artifact, work_dir=work_dir)
        )
        if kind is not None:
            artifact.kind = str(kind or "").strip().lower()
        if status is not None:
            artifact.status = ArtifactService.normalize_status(status)
        artifact.updated_seq = int(current_seq)
        ArtifactService.write_content_file(
            artifact,
            content=body,
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
