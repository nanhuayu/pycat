from __future__ import annotations

import copy
from typing import Any, List

from models.conversation import Conversation, Message

from core.prompts.history import get_effective_history
from core.prompts.user_context import (
    build_conversation_summary,
    build_environment_info,
    build_workspace_info,
)
from core.state.services.memory_service import MemoryService


def _synthetic_context_message(content: str, *, kind: str) -> Message:
    return Message(
        role="user",
        content=content,
        metadata={"context_kind": kind, "synthetic": True},
    )


def _latest_user_query(conversation: Conversation) -> str:
    for msg in reversed(getattr(conversation, "messages", []) or []):
        if getattr(msg, "role", "") != "user":
            continue
        content = str(getattr(msg, "content", "") or "").strip()
        if content:
            return content
    return ""


def _selected_memory_sources(conversation: Conversation) -> tuple[str, ...] | None:
    settings = getattr(conversation, "settings", {}) or {}
    return settings.get("memory_sources")


def build_context_messages(
    conversation: Conversation,
    *,
    app_config: Any,
    keep_last_turns: int,
    default_work_dir: str = ".",
) -> List[Message]:
    """Assemble Copilot-style runtime context.

    The resulting prompt is composed from three first-class sections:
    1. Environment and workspace metadata
    2. Historical summary from SessionState
    3. Recent complete conversation turns
    """
    prompt_cfg = getattr(app_config, "prompts", None)
    work_dir = getattr(conversation, "work_dir", None) or default_work_dir or "."
    max_depth = max(1, int(getattr(prompt_cfg, "file_tree_max_depth", 2) or 2))

    sections: List[Message] = []

    environment_blocks: List[str] = []
    if bool(getattr(prompt_cfg, "include_environment", True)):
        environment_blocks.append(build_environment_info(cwd=work_dir))

    workspace_block = build_workspace_info(work_dir, max_depth=max_depth)
    if workspace_block:
        environment_blocks.append(workspace_block)

    if environment_blocks:
        sections.append(
            _synthetic_context_message(
                "\n\n".join(block for block in environment_blocks if block.strip()),
                kind="environment",
            )
        )

    summary_block = build_conversation_summary(conversation)
    if summary_block:
        sections.append(_synthetic_context_message(summary_block, kind="summary"))

    memory_block = ""
    try:
        memory_block = MemoryService.build_prompt_section(
            conversation.get_state(),
            _latest_user_query(conversation),
            work_dir=str(work_dir or "."),
            sources=_selected_memory_sources(conversation),
        )
    except Exception:
        memory_block = ""
    if memory_block:
        sections.append(_synthetic_context_message(memory_block, kind="memory"))

    recent_history = [
        copy.deepcopy(msg)
        for msg in get_effective_history(
            conversation.messages,
            keep_last_turns=keep_last_turns,
        )
    ]
    return sections + recent_history
