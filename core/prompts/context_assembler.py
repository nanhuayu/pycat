from __future__ import annotations

import copy
from typing import Any, List

from models.conversation import Conversation, Message

from core.prompts.history import get_effective_history
from core.prompts.providers import ProviderContext, get_default_context_providers


def _latest_user_query(conversation: Conversation) -> str:
    for msg in reversed(getattr(conversation, "messages", []) or []):
        if getattr(msg, "role", "") != "user":
            continue
        content = str(getattr(msg, "content", "") or "").strip()
        if content:
            return content
    return ""


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
    work_dir = getattr(conversation, "work_dir", None) or default_work_dir or "."

    sections: List[Message] = []
    provider_context = ProviderContext(
        conversation=conversation,
        app_config=app_config,
        work_dir=str(work_dir or "."),
        latest_user_query=_latest_user_query(conversation),
    )
    for provider in get_default_context_providers():
        try:
            sections.extend(provider.build(provider_context))
        except Exception:
            continue

    recent_history = [
        copy.deepcopy(msg)
        for msg in get_effective_history(
            conversation.messages,
            keep_last_turns=keep_last_turns,
        )
    ]
    return sections + recent_history
