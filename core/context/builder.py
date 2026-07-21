from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, List

from core.context.history import get_effective_history
from core.context.items import ContextBudgetState, ContextItem, ContextPacker
from core.context.providers import ProviderContext, get_default_context_providers
from core.context.providers.memory import selected_memory_sources
from core.context.request_context_planner import RequestContextPlanner
from core.llm.token_budget import resolve_token_budget
from models.conversation import Conversation, Message


DEFAULT_PROVIDER_CONTEXT_LIMIT = 16_000


@dataclass(frozen=True)
class RuntimeContextSections:
    channel: str = ""
    project_instructions: str = ""
    skills: str = ""
    memory: str = ""


def latest_user_query(conversation: Conversation) -> str:
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
    memory_prompt: str = "",
) -> List[Message]:
    """Assemble provider context and recent conversation history."""
    work_dir = getattr(conversation, "work_dir", None) or default_work_dir or "."
    items: list[ContextItem] = []
    provider_context = ProviderContext(
        conversation=conversation,
        app_config=app_config,
        work_dir=str(work_dir or "."),
        latest_user_query=latest_user_query(conversation),
        memory_prompt=str(memory_prompt or ""),
        memory_sources=selected_memory_sources(conversation),
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


def prepare_context_messages(
    conversation: Conversation,
    context_window_limit: int,
    app_config: Any,
    *,
    keep_last_turns: int = 3,
    default_work_dir: str = ".",
    sections: RuntimeContextSections | None = None,
) -> List[Message]:
    del context_window_limit
    sections = sections or RuntimeContextSections()
    return build_context_messages(
        conversation=conversation,
        app_config=app_config,
        keep_last_turns=max(1, int(keep_last_turns or 3)),
        default_work_dir=default_work_dir,
        memory_prompt=sections.memory,
    )


def prepare_api_messages(
    messages: list[Message],
    *,
    conversation: Conversation | None,
    replay_pressure: str = "normal",
) -> tuple[list[Message], Any]:
    """Apply request replay planning and return the tool-result renderer."""
    prepared = copy.deepcopy(messages)
    planner = RequestContextPlanner(
        conversation=conversation,
        replay_pressure=replay_pressure,
    )
    planner.prepare_messages(prepared)
    return prepared, planner.render_tool_result
