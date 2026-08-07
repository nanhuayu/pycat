from __future__ import annotations

import hashlib
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import Iterable

from models.contracts.content import ContentRef
from models.conversation import Message, normalize_tool_result


def build_workspace_content_ref(
    work_dir: str | Path,
    path: str | Path,
    *,
    source: str = "agent",
) -> ContentRef:
    """Build a stable reference for an existing file inside one workspace."""
    raw_root = str(work_dir or "").strip()
    if not raw_root:
        raise ValueError("workspace references require an active workspace")
    root = Path(raw_root).expanduser().resolve()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"file is outside workspace: {path}") from exc
    if not resolved.is_file():
        raise ValueError(f"not a regular file: {path}")

    before = resolved.stat()
    digest = hashlib.sha256()
    with resolved.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    after = resolved.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f"file changed while preparing delivery: {path}")
    relative_ref = relative.as_posix()
    return ContentRef(
        id=relative_ref,
        name=resolved.name,
        mime=mimetypes.guess_type(resolved.name)[0] or "application/octet-stream",
        size=int(after.st_size),
        digest=digest.hexdigest(),
        ref=f"workspace:{relative_ref}",
        kind="workspace",
        source=str(source or "agent"),
        status="ready",
        created_at=datetime.now().isoformat(),
    )


def delivery_refs_for_messages(messages: Iterable[Message]) -> list[ContentRef]:
    """Collect explicitly delivered refs from successful Assistant tool results."""
    refs: list[ContentRef] = []
    seen: set[tuple[str, str, str]] = set()
    for message in messages or []:
        if not isinstance(message, Message) or message.role != "assistant":
            continue
        for tool_call in message.tool_calls or []:
            if not isinstance(tool_call, dict) or "result" not in tool_call:
                continue
            result = normalize_tool_result(tool_call.get("result"))
            metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
            if bool(result.get("is_error")) or bool(metadata.get("is_error")):
                continue
            for payload in metadata.get("content_refs") or []:
                if not isinstance(payload, dict):
                    continue
                try:
                    ref = ContentRef.from_dict(payload)
                except (TypeError, ValueError):
                    continue
                if ref.kind not in {"workspace", "artifact", "archive"}:
                    continue
                if not ref.ref or not ref.name:
                    continue
                key = (ref.kind, ref.ref, ref.digest)
                if key in seen:
                    continue
                seen.add(key)
                refs.append(ref)
    return refs
