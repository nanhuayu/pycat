from __future__ import annotations

import copy
from datetime import datetime
from typing import Any, List

from core.context.history import get_effective_history
from core.context.items import ContextItem, ContextPacker
from core.context.providers import ProviderContext, get_default_context_providers
from core.context.providers.memory import selected_memory_sources
from core.context.tool_replay_planner import ToolReplayPlanner
from models.conversation import Conversation, Message


DEFAULT_PROVIDER_CONTEXT_LIMIT = 16_000
SUMMARY_CONTEXT_KIND = "summary"
CURRENT_STATE_CONTEXT_KIND = "current_state"


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
    include_environment: bool = True,
    captured_at: datetime | None = None,
    prompt_limit: int = 0,
) -> List[Message]:
    """Assemble summary checkpoint, event history, and one tail state snapshot."""
    work_dir = getattr(conversation, "work_dir", None) or default_work_dir or "."
    snapshot_time = captured_at or datetime.now().astimezone()
    items: list[ContextItem] = []
    provider_context = ProviderContext(
        conversation=conversation,
        app_config=app_config,
        work_dir=str(work_dir or "."),
        latest_user_query=latest_user_query(conversation),
        memory_prompt=str(memory_prompt or ""),
        memory_sources=selected_memory_sources(conversation),
        include_environment=bool(include_environment),
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
    if prompt_limit > 0:
        token_limit = max(1, min(int(prompt_limit * 0.25), 64_000))
    packed_items = ContextPacker(token_limit).pack(items).items
    summary_messages = [item.to_message() for item in packed_items if item.kind == SUMMARY_CONTEXT_KIND]
    state_items = [item for item in packed_items if item.kind != SUMMARY_CONTEXT_KIND]

    recent_history = [
        copy.deepcopy(msg)
        for msg in get_effective_history(
            conversation.messages,
            keep_last_turns=keep_last_turns,
        )
    ]
    messages = summary_messages + recent_history
    if state_items:
        captured_text = snapshot_time.astimezone().replace(second=0, microsecond=0).isoformat(timespec="minutes")
        state_content = "\n\n".join(item.content.strip() for item in state_items if item.content.strip())
        messages.append(
            Message(
                role="user",
                content=(
                    f'<current_state captured_at="{captured_text}">\n'
                    f"{state_content}\n"
                    "</current_state>"
                ),
                metadata={
                    "synthetic": True,
                    "context_kind": CURRENT_STATE_CONTEXT_KIND,
                    "context_sections": [item.kind for item in state_items],
                    "context_item_ids": [item.id for item in state_items],
                    "captured_at": captured_text,
                },
            )
        )
    return messages


def prepare_context_messages(
    conversation: Conversation,
    app_config: Any,
    *,
    keep_last_turns: int = 3,
    default_work_dir: str = ".",
    memory_prompt: str = "",
    include_environment: bool = True,
    captured_at: datetime | None = None,
    prompt_limit: int = 0,
) -> List[Message]:
    return build_context_messages(
        conversation=conversation,
        app_config=app_config,
        keep_last_turns=max(1, int(keep_last_turns or 3)),
        default_work_dir=default_work_dir,
        memory_prompt=memory_prompt,
        include_environment=include_environment,
        captured_at=captured_at,
        prompt_limit=prompt_limit,
    )


def prepare_api_messages(
    messages: list[Message],
    *,
    conversation: Conversation | None,
    replay_pressure: str = "normal",
) -> tuple[list[Message], Any]:
    """Apply request replay planning and return the tool-result renderer."""
    prepared = copy.deepcopy(messages)
    planner = ToolReplayPlanner(
        conversation=conversation,
        replay_pressure=replay_pressure,
    )
    planner.prepare_messages(prepared)
    return prepared, planner.render_tool_result
