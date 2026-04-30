"""Helpers for structured runtime context sections.

The primary request path now assembles environment, summary, and recent
history as separate first-class messages. The legacy inject/wrap helpers are
kept for compatibility and for normalizing older persisted messages.
"""
from __future__ import annotations

import os
import platform
import re
from datetime import datetime
from dataclasses import replace
from typing import List

from models.conversation import Conversation, Message
from core.context.file_context import get_file_tree


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


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def build_environment_info(*, cwd: str | None = None) -> str:
    """OS / shell / date — always available."""
    os_name = platform.system()
    os_release = platform.release()
    os_version = platform.version()
    machine = platform.machine() or "unknown"
    shell = os.environ.get("SHELL") or os.environ.get("COMSPEC") or "unknown"
    cwd = os.path.abspath(cwd or os.getcwd())
    now = datetime.now().astimezone()
    lines = [
        f"<environment_info>",
        f"OS: {os_name} {os_release}",
        f"OS Version: {os_version}",
        f"Machine: {machine}",
        f"Shell: {shell}",
        f"Current Time: {now.isoformat(timespec='seconds')}",
        f"CWD: {cwd}",
        f"</environment_info>",
    ]
    return "\n".join(lines)


def build_workspace_info(work_dir: str, *, max_depth: int = 2) -> str:
    """File tree of the workspace — may be empty for non-project chats."""
    abs_dir = os.path.abspath(work_dir)
    tree = get_file_tree(abs_dir, max_depth=max_depth)
    if not tree or not tree.strip():
        return ""
    return f"<workspace_info>\n{tree.strip()}\n</workspace_info>"


def build_conversation_summary(conversation: Conversation) -> str:
    """Condensed summary from SessionState, if any."""
    try:
        state = conversation.get_state()
        summary = getattr(state, "summary", "") or ""
        if not summary.strip():
            return ""
        return f"<conversation_summary>\n{summary.strip()}\n</conversation_summary>"
    except Exception:
        return ""


def extract_user_request(content: str) -> str:
    """Strip injected runtime tags and recover the pure user request text."""
    raw = str(content or "")
    match = _USER_REQUEST_RE.search(raw)
    if match:
        return match.group(1).strip()
    return _RUNTIME_BLOCK_RE.sub("", raw).strip()


def normalize_user_message(message: Message) -> Message:
    """Return a copy of a user message with runtime context removed from content."""
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
    """Build the ephemeral runtime context attached to the latest user request."""
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
    """Wrap a pure user request with runtime context tags."""
    request = extract_user_request(content)
    if not context_block:
        return request
    return f"{context_block}\n<user_request>\n{request}\n</user_request>"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def inject_user_context(
    conversation: Conversation,
    *,
    include_environment: bool = True,
    include_workspace: bool = True,
    include_summary: bool = True,
    inject_mode: str = "first",
) -> None:
    """Inject XML context sections into user message(s) **in-place**.

    Parameters
    ----------
    inject_mode
        ``"first"`` — inject into the first user message only.
        ``"latest"`` — inject into the last user message only.
        ``"all"`` — inject into every user message (not recommended).
    """
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MARKER = "<environment_info>"


def _already_injected(msg: Message) -> bool:
    return bool(msg.content and (_MARKER in msg.content or "<user_request>" in msg.content))


def _prepend_context(msg: Message, block: str) -> None:
    msg.content = wrap_user_request(msg.content or "", block)
