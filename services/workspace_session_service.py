from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any, Dict

from core.config import get_global_subdir
from models.conversation import Conversation


logger = logging.getLogger(__name__)


def _safe_slug(value: str, *, fallback: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip()).strip("-._")
    return text or fallback


class WorkspaceSessionService:
    """Mirror conversation state into stable workspace-scoped session folders."""

    def __init__(self, root_dir: str | Path | None = None) -> None:
        self.root_dir = Path(root_dir) if root_dir else get_global_subdir("workspace_sessions")
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def save_snapshot(self, conversation: Conversation) -> None:
        session_dir = self._get_session_dir(conversation)
        session_dir.mkdir(parents=True, exist_ok=True)

        state = conversation.get_state()
        llm_config = conversation.get_llm_config()
        settings = conversation.settings or {}
        workspace_dir = session_dir.parent

        self._write_json(
            workspace_dir / "workspace.json",
            {
                "workspace": str(getattr(conversation, "work_dir", "") or "."),
                "workspace_key": workspace_dir.name,
            },
        )

        self._write_json(
            session_dir / "meta.json",
            {
                "conversation_id": conversation.id,
                "title": conversation.title,
                "provider_id": llm_config.provider_id or conversation.provider_id,
                "provider_name": llm_config.provider_name or getattr(conversation, "provider_name", ""),
                "model": llm_config.model or conversation.model,
                "llm_config": llm_config.to_dict(),
                "mode": getattr(conversation, "mode", "") or "chat",
                "work_dir": str(getattr(conversation, "work_dir", "") or "."),
                "updated_at": getattr(conversation, "updated_at", None).isoformat()
                if getattr(conversation, "updated_at", None)
                else "",
                "show_thinking": bool(settings.get("show_thinking", True)),
            },
        )
        self._write_json(session_dir / "state.json", state.to_dict())
        self._copy_artifact_files(session_dir, state.to_dict(), work_dir=str(getattr(conversation, "work_dir", "") or "."))
        self._copy_tool_result_files(session_dir, conversation)
        self._cleanup_legacy_artifacts(session_dir)

    def delete_snapshot(self, conversation_id: str, *, work_dir: str | None = None) -> None:
        for session_dir in self._find_session_dirs(conversation_id, work_dir=work_dir):
            try:
                shutil.rmtree(session_dir, ignore_errors=True)
            except Exception:
                continue

            workspace_dir = session_dir.parent
            try:
                workspace_meta = workspace_dir / "workspace.json"
                remaining_children = [child for child in workspace_dir.iterdir()]
                if workspace_meta in remaining_children and len(remaining_children) == 1:
                    workspace_meta.unlink(missing_ok=True)
                    remaining_children = []
                if workspace_dir.exists() and not remaining_children:
                    workspace_dir.rmdir()
            except Exception:
                continue

    def _find_session_dirs(self, conversation_id: str, *, work_dir: str | None = None) -> list[Path]:
        clean_id = str(conversation_id or "").strip()
        if not clean_id:
            return []
        if work_dir:
            return [self._get_workspace_dir(work_dir) / clean_id]
        return [path for path in self.root_dir.glob(f"*/{clean_id}") if path.is_dir()]

    def _get_session_dir(self, conversation: Conversation) -> Path:
        work_dir = str(getattr(conversation, "work_dir", "") or ".")
        return self._get_workspace_dir(work_dir) / str(conversation.id)

    def _get_workspace_dir(self, work_dir: str) -> Path:
        local_root = self._resolve_local_workspace_root(work_dir)
        if local_root is not None:
            local_root.mkdir(parents=True, exist_ok=True)
            return local_root

        raw = str(work_dir or ".")
        try:
            resolved = str(Path(raw).expanduser().resolve())
        except Exception:
            resolved = raw
        leaf = _safe_slug(Path(resolved).name, fallback="workspace")
        digest = hashlib.sha1(resolved.lower().encode("utf-8")).hexdigest()[:12]
        workspace_dir = self.root_dir / f"{leaf}-{digest}"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        return workspace_dir

    @staticmethod
    def _resolve_local_workspace_root(work_dir: str) -> Path | None:
        raw = str(work_dir or "").strip()
        if not raw:
            return None
        try:
            candidate = Path(raw).expanduser().resolve()
        except Exception:
            return None
        if not candidate.exists() or not candidate.is_dir():
            return None
        return candidate / ".pycat" / "sessions"

    @staticmethod
    def _write_json(path: Path, payload: Dict[str, Any] | list[Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _cleanup_legacy_artifacts(session_dir: Path) -> None:
        legacy_files = [
            session_dir / "tasks.json",
            session_dir / "memory.json",
            session_dir / "summary.md",
        ]
        for path in legacy_files:
            try:
                path.unlink(missing_ok=True)
            except Exception as exc:
                logger.debug("Failed to remove legacy workspace session file %s: %s", path, exc)
                continue

        docs_dir = session_dir / "documents"
        if docs_dir.exists():
            try:
                shutil.rmtree(docs_dir, ignore_errors=True)
            except Exception as exc:
                logger.debug("Failed to remove legacy workspace session documents dir %s: %s", docs_dir, exc)

    @staticmethod
    def _copy_artifact_files(session_dir: Path, state_payload: Dict[str, Any], *, work_dir: str) -> None:
        artifacts = state_payload.get("artifacts") if isinstance(state_payload, dict) else {}
        if not isinstance(artifacts, dict):
            return
        workspace_root = Path(work_dir or ".").expanduser().resolve()
        target_dir = session_dir / "artifacts"
        for artifact in artifacts.values():
            if not isinstance(artifact, dict):
                continue
            rel_path = str(artifact.get("content_path") or "").strip()
            if not rel_path:
                continue
            source = Path(rel_path)
            if not source.is_absolute():
                source = workspace_root / source
            if not source.exists() or not source.is_file():
                continue
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target_dir / source.name)
            except Exception as exc:
                logger.debug("Failed to copy artifact file %s: %s", source, exc)

    @staticmethod
    def _copy_tool_result_files(session_dir: Path, conversation: Conversation) -> None:
        target_dir = session_dir / "tool-results"
        seen: set[Path] = set()
        for message in getattr(conversation, "messages", []) or []:
            metadata = getattr(message, "metadata", {}) or {}
            if not isinstance(metadata, dict):
                continue
            source_text = str(metadata.get("tool_result_file") or "").strip()
            if not source_text:
                continue
            source = Path(source_text)
            try:
                source = source.expanduser().resolve()
            except Exception:
                continue
            if source in seen or not source.is_file():
                continue
            seen.add(source)
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                dest = target_dir / source.name
                if source != dest.resolve():
                    shutil.copy2(source, dest)
            except Exception as exc:
                logger.debug("Failed to copy tool result file %s: %s", source, exc)