from __future__ import annotations

import hashlib
import ntpath
import os
import posixpath
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Iterable

from pycat.core.content.mime import guess_mime
from pycat.models.contracts.content import ContentRef, FileChange
from pycat.models.contracts.session_state import SessionArtifact
from pycat.models.conversation import Message, normalize_tool_result
from pycat.models.session_paths import normalize_work_dir
from pycat.models.workspace import WorkspaceLocation, workspace_identity


def content_identity(ref: ContentRef, *, work_dir: str = "", conversation_id: str = "") -> tuple:
    """Lexical owner/version identity. Never opens or hashes a content file."""
    workspace = normalize_work_dir(ref.workspace or work_dir)
    workspace = workspace_identity(workspace)
    owner = ref.conversation_id or conversation_id if ref.kind in {"input", "artifact", "archive"} else ""
    identifier = ref.id or ref.ref
    if ref.kind == "workspace":
        path = ref.ref.removeprefix("workspace:")
        location = WorkspaceLocation.parse(workspace)
        if location.is_remote:
            path_ops = ntpath if location.is_windows else posixpath
            identifier = path_ops.normcase(path_ops.normpath(path_ops.join(location.root, path)))
        else:
            identifier = os.path.normcase(os.path.normpath(
                path if os.path.isabs(path) else os.path.join(workspace, path)))
    return (ref.kind, workspace, owner, identifier, ref.digest,
            ref.locator if ref.kind == "archive" else "")


def material_rows(conversation) -> list[dict]:
    """Project current session metadata once, merging only known versions."""
    rows = {}
    work_dir, conversation_id = conversation.work_dir, conversation.id

    def add(ref, role, *, origin="", summary="", change=None):
        ref = replace(ref, workspace=ref.workspace or normalize_work_dir(work_dir),
                      conversation_id=ref.conversation_id or (
                          conversation_id if ref.kind in {"input", "artifact", "archive"} else ""))
        key = content_identity(ref)
        if not ref.digest:
            key += (origin or role,)
        row = rows.get(key)
        if row is None:
            row = {"key": key, "id": ref.id, "kind": "artifact" if ref.kind == "artifact" else "file",
                   "title": ref.id if ref.kind == "artifact" else ref.name,
                   "summary": summary, "ref": ref.to_dict(), "roles": [], "scope": "当前会话"}
            rows[key] = row
        if role not in row["roles"]:
            row["roles"].append(role)
        if change:
            row["change"] = change
        return row

    for name, artifact in conversation.get_state().artifacts.items():
        add(build_artifact_content_ref(artifact, work_dir=work_dir, conversation_id=conversation_id),
            "成果", origin=f"artifact:{name}", summary=artifact.abstract)
    for ref in delivery_refs_for_messages(conversation.messages):
        add(ref, "交付")
    for message in reversed(conversation.messages):
        for ref in message.content_refs or []:
            add(ref, "输入", origin=f"{message.id}:{ref.ref}")
    for change in file_changes_for_messages(conversation.messages):
        path = change["path"]
        ref = ContentRef(id=path, name=os.path.basename(path.replace("\\", "/")),
                         kind="workspace", ref=f"workspace:{path}", workspace=work_dir,
                         mime="", size=0, digest=change["after_digest"])
        row = add(ref, change["label"], origin=change["change_id"] or path, change=change)
        if change["deleted"]:
            row["ref"] = None
    return list(rows.values())


def file_changes_for_messages(messages: Iterable[Message]) -> list[dict]:
    """Project committed tool receipts; existence is checked when opening a file."""
    latest = {}
    labels = {"write": "已写入", "edit": "已编辑", "patch": "已应用补丁", "delete": "已删除"}
    for message in reversed(list(messages or [])):
        for call in reversed(message.tool_calls or []):
            result = call.get("result") if isinstance(call, dict) else None
            if not isinstance(result, dict) or result.get("is_error"):
                continue
            metadata = result.get("metadata") or {}
            data = metadata.get("file_change") if isinstance(metadata, dict) else None
            if not isinstance(data, dict):
                continue
            try:
                change = FileChange.from_dict(data)
            except (TypeError, ValueError):
                continue
            if change.is_successful and change.path and change.path not in latest:
                latest[change.path] = {**change.to_dict(), "label": labels.get(change.action, "已变更"),
                                       "deleted": change.action == "delete"}
    return list(latest.values())


def build_workspace_content_ref(
    work_dir: str | Path,
    path: str | Path,
    *,
    source: str = "agent",
    workspace_service=None,
) -> ContentRef:
    """Build a stable reference for an existing file inside one workspace."""
    raw_root = normalize_work_dir(work_dir)
    if not raw_root:
        raise ValueError("workspace references require an active workspace")
    if workspace_service is not None and (files := workspace_service.files(raw_root)):
        metadata = files.stat(path, digest=True)
        if not metadata["file"]:
            raise ValueError(f"not a regular file: {path}")
        relative = files.absolute(metadata["path"]).relative_to(files.location.root).as_posix()
        return ContentRef(id=relative, name=files.absolute(path).name,
            mime=guess_mime(str(path)),
            size=metadata["size"], digest=metadata["digest"], ref=f"workspace:{relative}",
            kind="workspace", source=source, status="ready", created_at=datetime.now().isoformat(), workspace=raw_root)
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
        mime=guess_mime(resolved.name),
        size=int(after.st_size),
        digest=digest.hexdigest(),
        ref=f"workspace:{relative_ref}",
        kind="workspace",
        source=str(source or "agent"),
        status="ready",
        created_at=datetime.now().isoformat(),
        workspace=str(root),
    )


def build_artifact_content_ref(
    artifact: SessionArtifact,
    *,
    source: str = "agent",
    work_dir: str = "",
    conversation_id: str = "",
) -> ContentRef:
    """Build a navigation reference for an Artifact without duplicating its body."""
    name = str(getattr(artifact, "name", "") or "").strip()
    if not name:
        raise ValueError("artifact name is required")
    return ContentRef(
        id=name,
        name=f"{name}.md",
        mime="text/markdown",
        size=int(getattr(artifact, "content_chars", 0) or 0),
        digest=str(getattr(artifact, "content_digest", "") or ""),
        ref=f"artifact:{name}",
        kind="artifact",
        source=str(source or "agent"),
        status=str(getattr(artifact, "status", "") or "draft"),
        workspace=normalize_work_dir(work_dir),
        conversation_id=conversation_id,
        locator=str(getattr(artifact, "content_path", "") or ""),
    )


def latest_turn_deliveries(conversation) -> list[ContentRef]:
    """Only receipts from the final user turn belong to a run's delivery result."""
    messages = conversation.messages
    start = next((index + 1 for index in range(len(messages) - 1, -1, -1)
                  if messages[index].role == 'user'), 0)
    return delivery_refs_for_messages(messages[start:])


def delivery_refs_for_messages(messages: Iterable[Message]) -> list[ContentRef]:
    """Collect explicitly delivered refs from successful Assistant tool results."""
    refs: list[ContentRef] = []
    seen: set[tuple] = set()
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
                key = content_identity(ref)
                if key in seen:
                    continue
                seen.add(key)
                refs.append(ref)
    return refs
