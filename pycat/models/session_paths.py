"""Single resolution rule for per-conversation session storage roots.

Every large runtime artifact of a conversation lives under one session root:

    artifact/   artifact markdown bodies
    history/    compacted history originals
    tool-call/  archived tool results (incl. MCP output)
    debug/      debug trace events and attachments
    process/    background shell logs
    input/      immutable user-selected input snapshots

All producers (archive store, artifact service, debug trace, MCP proxies,
process manager) must call :func:`resolve_session_root` instead of joining
``.pycat/sessions`` themselves.
"""
from __future__ import annotations

import os
import hashlib
from pathlib import Path

from pycat.models.workspace import WorkspaceLocation, workspace_identity


def normalize_work_dir(work_dir: str | Path | None) -> str:
    """Normalize the persisted workspace sentinel without resolving real paths."""

    raw = str(work_dir or "").strip()
    sentinel = raw.replace("\\", "/").rstrip("/")
    return "" if sentinel in {"", "."} else WorkspaceLocation.parse(raw).value


def resolve_project_data_root(work_dir: str | Path | None, *, data_dir: str | Path | None = None) -> Path:
    """PyCat-owned project data stays local even when the workspace is remote."""
    location = WorkspaceLocation.parse(normalize_work_dir(work_dir))
    root = Path(data_dir) if data_dir else Path.home() / ".pycat"
    if location.is_remote:
        return root / "remote-workspaces" / hashlib.sha256(workspace_identity(location.value).encode()).hexdigest()[:24]
    return Path(location.root).expanduser() / ".pycat" if location.root else root


def has_active_workspace(work_dir: str | Path | None) -> bool:
    return bool(normalize_work_dir(work_dir))


def resolve_session_root(work_dir: str | Path | None, session_id: str, *, data_dir: str | Path | None = None) -> Path:
    """Return the canonical session root without touching the filesystem.

    Rules (no fallback to "."):
    - local workspace: ``<work_dir>/.pycat/sessions/<session_id>``
    - SSH workspace: ``<data_dir>/remote-workspaces/<hash>/sessions/<session_id>``
    - without workspace: ``<data_dir>/sessions/<session_id>`` (default ~/.pycat)

    ``session_id`` must be a safe single path segment; absolute paths, ``..``
    and path separators are rejected.
    """
    clean_id = str(session_id or "").strip()
    if (
        not clean_id
        or clean_id in {".", ".."}
        or os.path.isabs(clean_id)
        or "/" in clean_id
        or "\\" in clean_id
        or (os.path.altsep and os.path.altsep in clean_id)
    ):
        raise ValueError(f"unsafe session id: {session_id!r}")
    return resolve_project_data_root(work_dir, data_dir=data_dir) / "sessions" / clean_id
