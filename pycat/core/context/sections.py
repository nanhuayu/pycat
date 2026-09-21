"""Runtime context section builders and user-request normalization."""
from __future__ import annotations

import os
import platform
import re
from dataclasses import replace
from datetime import datetime
from typing import List

from pycat.core.context.file_context import get_file_tree
from pycat.models.conversation import Conversation, Message
from pycat.models.workspace import WorkspaceLocation


RUNTIME_CONTEXT_TAGS = (
    "environment_info",
    "workspace_info",
    "conversation_summary",
    "relevant_memory",
    "active_channels",
)
_USER_REQUEST_RE = re.compile(r"<user_request>\s*(.*?)\s*</user_request>", re.DOTALL)
_RUNTIME_BLOCK_RE = re.compile(
    r"<(environment_info|workspace_info|conversation_summary|relevant_memory|active_channels)>.*?</\1>\s*",
    re.DOTALL,
)


def _iso_time_text(value: datetime) -> str:
    localized = value if value.tzinfo is not None else value.astimezone()
    return localized.isoformat(timespec="seconds")


def build_environment_info(
    *,
    cwd: str | None = None,
    now: datetime | None = None,
    include_time: bool = True,
) -> str:
    """Build the OS, shell, and working-directory context section."""
    location = WorkspaceLocation.parse(cwd)
    if location.is_remote:
        lines = ["<environment_info>", f"OS: {'Windows' if location.is_windows else 'Linux'} (SSH workspace)",
                 f"Shell: {'cmd.exe' if location.is_windows else '/bin/sh'}",
                 f"Host: {location.endpoint}", f"CWD: {location.root}",
                 "File and shell tools act on this remote host. Conversation inputs are uploaded under .pycat/inputs/.",
                 "Desktop, browser and MCP tools still run on the local host unless explicitly configured otherwise."]
        if include_time:
            lines.append(f"Current Time: {_iso_time_text(now or datetime.now().astimezone())}")
        return "\n".join([*lines, "</environment_info>"])
    os_name = platform.system()
    os_release = platform.release()
    os_version = platform.version()
    machine = platform.machine() or "unknown"
    shell = os.environ.get("SHELL") or os.environ.get("COMSPEC") or "unknown"
    raw_cwd = None if cwd is None else str(cwd).strip()
    resolved_cwd = os.path.abspath(os.getcwd() if raw_cwd is None else raw_cwd) if raw_cwd is None or raw_cwd else ""
    lines = [
        "<environment_info>",
        f"OS: {os_name} {os_release}",
        f"OS Version: {os_version}",
        f"Machine: {machine}",
        f"Shell: {shell}",
    ]
    if include_time:
        lines.append(f"Current Time: {_iso_time_text(now or datetime.now().astimezone())}")
    if resolved_cwd:
        lines.append(f"CWD: {resolved_cwd}")
    else:
        lines.append("Workspace: not selected")
    lines.append("</environment_info>")
    return "\n".join(lines)


def build_workspace_info(work_dir: str, *, max_depth: int = 2) -> str:
    """Build a compact workspace tree context section."""
    raw_work_dir = str(work_dir or "").strip()
    if not raw_work_dir:
        return ""
    if WorkspaceLocation.parse(raw_work_dir).is_remote:
        return "<workspace_info>SSH workspace; use file__list to inspect the remote directory.</workspace_info>"
    abs_dir = os.path.abspath(raw_work_dir)
    tree = get_file_tree(abs_dir, max_depth=max_depth)
    if not tree or not tree.strip():
        return ""
    return f"<workspace_info>\n{tree.strip()}\n</workspace_info>"


def build_conversation_summary(conversation: Conversation) -> str:
    """Build the rolling conversation summary section, if present."""
    try:
        state = conversation.get_state()
        summary = getattr(state, "summary", "") or ""
        if not summary.strip():
            return ""
        return f"<conversation_summary>\n{summary.strip()}\n</conversation_summary>"
    except Exception:
        return ""


def extract_user_request(content: str) -> str:
    """Strip injected runtime context tags and return the plain user request."""
    raw = str(content or "")
    match = _USER_REQUEST_RE.search(raw)
    if match:
        return match.group(1).strip()
    return _RUNTIME_BLOCK_RE.sub("", raw).strip()


def normalize_user_message(message: Message) -> Message:
    """Return a copy of a user message with runtime context removed."""
    if message.role != "user":
        return message
    return replace(message, content=extract_user_request(message.content))


def build_runtime_context_block(
    conversation: Conversation,
    *,
    include_environment: bool = True,
    include_workspace: bool = True,
    include_summary: bool = False,
    max_depth: int = 2,
) -> str:
    """Build the ephemeral runtime context block for a user request."""
    work_dir = str(getattr(conversation, "work_dir", "") or "").strip()
    sections: List[str] = []
    if include_environment:
        sections.append(build_environment_info(cwd=work_dir))
    if include_workspace:
        workspace_info = build_workspace_info(work_dir, max_depth=max_depth)
        if workspace_info:
            sections.append(workspace_info)
    if include_summary:
        summary = build_conversation_summary(conversation)
        if summary:
            sections.append(summary)
    return "\n".join(sections).strip()


def wrap_user_request(content: str, context_block: str) -> str:
    """Wrap a plain user request with runtime context tags."""
    request = extract_user_request(content)
    if not context_block:
        return request
    return f"{context_block}\n<user_request>\n{request}\n</user_request>"


def inject_user_context(
    conversation: Conversation,
    *,
    include_environment: bool = True,
    include_workspace: bool = True,
    include_summary: bool = True,
    inject_mode: str = "first",
) -> None:
    """Inject XML-like context sections into user messages in place."""
    context_block = build_runtime_context_block(
        conversation,
        include_environment=include_environment,
        include_workspace=include_workspace,
        include_summary=include_summary,
    )
    if not context_block:
        return

    messages = conversation.messages or []
    if inject_mode == "first":
        for msg in messages:
            if msg.role == "user" and not _already_injected(msg):
                _prepend_context(msg, context_block)
                break
    elif inject_mode == "latest":
        for msg in reversed(messages):
            if msg.role == "user" and not _already_injected(msg):
                _prepend_context(msg, context_block)
                break
    elif inject_mode == "all":
        for msg in messages:
            if msg.role == "user" and not _already_injected(msg):
                _prepend_context(msg, context_block)


_MARKER = "<environment_info>"


def _already_injected(msg: Message) -> bool:
    return bool(msg.content and (_MARKER in msg.content or "<user_request>" in msg.content))


def _prepend_context(msg: Message, block: str) -> None:
    msg.content = wrap_user_request(msg.content or "", block)
