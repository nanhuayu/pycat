from __future__ import annotations

import copy
from typing import Any, List

from core.context.items import ContextBudgetState, ContextItem, ContextPacker
from core.llm.token_budget import resolve_token_budget
from models.conversation import Conversation, Message

from core.prompts.history import get_effective_history
from core.prompts.providers import ProviderContext, get_default_context_providers


DEFAULT_PROVIDER_CONTEXT_LIMIT = 16_000


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

    # Keep prompt assembly easy to reason about:
    # summary -> todo/work_trace/memory -> artifacts/archive index -> recent history.
    items: list[ContextItem] = []
    provider_context = ProviderContext(
        conversation=conversation,
        app_config=app_config,
        work_dir=str(work_dir or "."),
        latest_user_query=_latest_user_query(conversation),
    )
    for provider in get_default_context_providers():
        try:
            provider_items = provider.build_items(provider_context) if hasattr(provider, "build_items") else []
            if provider_items:
                items.extend(provider_items)
                continue
            items.extend(
                ContextItem(
                    id=str(message.metadata.get("context_kind") or getattr(provider, "name", "context")),
                    kind=str(message.metadata.get("context_kind") or getattr(provider, "name", "context")),
                    content=message.content,
                    priority=int(getattr(provider, "priority", 100)),
                    metadata=dict(message.metadata or {}),
                )
                for message in provider.build(provider_context)
            )
        except Exception:
            continue
    token_limit = DEFAULT_PROVIDER_CONTEXT_LIMIT
    try:
        budget = resolve_token_budget(conversation=conversation)
        token_limit = max(DEFAULT_PROVIDER_CONTEXT_LIMIT, min(int(budget.effective_prompt_limit * 0.25), 64_000))
    except Exception:
        token_limit = DEFAULT_PROVIDER_CONTEXT_LIMIT
    messages = ContextPacker(ContextBudgetState(token_limit=token_limit)).pack(items).to_messages()

    recent_history = [
        copy.deepcopy(msg)
        for msg in get_effective_history(
            conversation.messages,
            keep_last_turns=keep_last_turns,
        )
    ]
    return messages + recent_history
