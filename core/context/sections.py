"""Runtime context section builders and user-request normalization."""
from __future__ import annotations

import os
import platform
import re
from dataclasses import replace
from datetime import datetime
from typing import List

from core.context.file_context import get_file_tree
from models.conversation import Conversation, Message


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
) -> str:
    """Build the OS, shell, time, and working-directory context section."""
    os_name = platform.system()
    os_release = platform.release()
    os_version = platform.version()
    machine = platform.machine() or "unknown"
    shell = os.environ.get("SHELL") or os.environ.get("COMSPEC") or "unknown"
    cwd = os.path.abspath(cwd or os.getcwd())
    lines = [
        "<environment_info>",
        f"OS: {os_name} {os_release}",
        f"OS Version: {os_version}",
        f"Machine: {machine}",
        f"Shell: {shell}",
        f"Current Time: {_iso_time_text(now or datetime.now().astimezone())}",
        f"CWD: {cwd}",
        "</environment_info>",
    ]
    return "\n".join(lines)


def build_workspace_info(work_dir: str, *, max_depth: int = 2) -> str:
    """Build a compact workspace tree context section."""
    abs_dir = os.path.abspath(work_dir)
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
    work_dir = getattr(conversation, "work_dir", None) or "."
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
