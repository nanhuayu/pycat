"""Small helpers for recording successful workspace file mutations."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from models.contracts.content import FileChange


def digest_bytes(data: bytes) -> str:
    """Return the SHA-256 digest used by the FileChange contract."""
    return hashlib.sha256(bytes(data or b"")).hexdigest()


def digest_path(path: Path) -> str:
    """Hash one regular file without loading it all into memory."""
    try:
        if not path.is_file():
            return ""
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, ValueError):
        return ""


def file_change_metadata(
    *,
    path: str,
    action: str,
    context: Any,
    before_digest: str = "",
    after_digest: str = "",
    summary: str = "",
) -> dict[str, Any]:
    """Build the nested ToolResult metadata for a completed mutation."""
    runtime = getattr(context, "runtime", None)
    debug_trace = getattr(runtime, "debug_trace", None)
    run_id = str(
        getattr(runtime, "run_id", "")
        or getattr(debug_trace, "request_id", "")
        or getattr(runtime, "trace_id", "")
        or ""
    )
    tool_call_id = str(getattr(runtime, "tool_call_id", "") or "")
    change = FileChange(
        change_id=uuid4().hex,
        path=str(path or "").strip(),
        action=action,
        before_digest=before_digest,
        after_digest=after_digest,
        run_id=run_id,
        tool_call_id=tool_call_id,
        created_at=datetime.now(timezone.utc).isoformat(),
        status="completed",
        summary=str(summary or "").strip()[:500],
    )
    return {"file_change": change.to_dict()}
