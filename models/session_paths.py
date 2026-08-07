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
from pathlib import Path


def resolve_session_root(work_dir: str | Path | None, session_id: str) -> Path:
    """Return the canonical session root without touching the filesystem.

    Rules (no fallback to "."):
    - with workspace: ``<work_dir>/.pycat/sessions/<session_id>``
    - without workspace: ``<user_home>/.pycat/sessions/<session_id>``

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
    raw = str(work_dir or "").strip()
    base = Path(raw).expanduser() if raw else Path.home()
    return base / ".pycat" / "sessions" / clean_id
