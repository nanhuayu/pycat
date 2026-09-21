from __future__ import annotations

import copy
from datetime import datetime
from typing import Any, List

from pycat.core.content.archive_store import SessionArchiveStore
from pycat.core.context.history import (
    build_turn_blocks,
    get_effective_history,
    is_real_user_message,
    turn_fingerprint,
)
from pycat.core.context.items import ContextItem, ContextPacker
from pycat.core.context.providers import ProviderContext, get_default_context_providers
from pycat.core.context.providers.memory import memory_enabled
from pycat.core.context.tool_replay_planner import ToolReplayPlanner
from pycat.models.conversation import Conversation, Message


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
    default_work_dir: str = "",
    memory_prompt: str = "",
    include_environment: bool = True,
    captured_at: datetime | None = None,
    prompt_limit: int = 0,
    visible_tool_names: set[str] | None = None,
) -> List[Message]:
    """Assemble summary checkpoint, event history, and one tail state snapshot."""
    work_dir = str(getattr(conversation, "work_dir", "") or default_work_dir or "").strip()
    snapshot_time = captured_at or datetime.now().astimezone()
    items: list[ContextItem] = []
    provider_context = ProviderContext(
        conversation=conversation,
        app_config=app_config,
        work_dir=work_dir,
        latest_user_query=latest_user_query(conversation),
        memory_prompt=str(memory_prompt or ""),
        memory_enabled=memory_enabled(conversation),
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

    if visible_tool_names is not None and "archive__read" not in visible_tool_names:
        items = [item for item in items if item.kind != "archive"]

    token_limit = DEFAULT_PROVIDER_CONTEXT_LIMIT
    if prompt_limit > 0:
        token_limit = max(1, min(int(prompt_limit * 0.25), 64_000))
    packed_items = ContextPacker(token_limit).pack(items).items
    summary_messages = [item.to_message() for item in packed_items if item.kind == SUMMARY_CONTEXT_KIND]
    state_items = [item for item in packed_items if item.kind != SUMMARY_CONTEXT_KIND]

    recent_history = _project_history(
        conversation,
        visible_tool_names=visible_tool_names,
    )
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


def _project_history(
    conversation: Conversation,
    *,
    visible_tool_names: set[str] | None = None,
) -> list[Message]:
    """Project exact history plus selected dormant turn capsules."""
    store = SessionArchiveStore(conversation.work_dir, conversation.id, data_dir=getattr(conversation, "data_dir", None))
    projected: list[Message] = []
    for block in build_turn_blocks(list(conversation.messages or [])):
        if not block or not is_real_user_message(block[0]):
            projected.extend(copy.deepcopy(message) for message in get_effective_history(block))
            continue
        user = block[0]
        if user.archived_content_id:
            continue
        ref = (user.metadata or {}).get("turn_capsule_ref") if isinstance(user.metadata, dict) else None
        content_id = str(ref.get("content_id") or "") if isinstance(ref, dict) else ""
        tail = block[1:]
        selected = bool(tail and content_id) and all(
            str(getattr(message, "archived_content_id", "") or "") == content_id
            for message in tail
        )
        record = store.read_record(content_id, kind="history") if selected else None
        metadata = record.metadata if record is not None and isinstance(record.metadata, dict) else {}
        fingerprint = turn_fingerprint(block)
        valid = bool(
            record is not None
            and record.summary
            and metadata.get("scope") == "turn_capsule"
            and metadata.get("fingerprint") == fingerprint
            and isinstance(ref, dict)
            and ref.get("fingerprint") == fingerprint
        )
        if not valid:
            exact = copy.deepcopy(block)
            for message in exact:
                if message is not exact[0] and str(message.archived_content_id or "") == content_id:
                    message.archived_content_id = None
            projected.extend(get_effective_history(exact))
            continue
        projected.extend(get_effective_history([copy.deepcopy(user)]))
        projected.append(
            Message(
                role="assistant",
                content=record.summary,
                metadata={
                    "synthetic": True,
                    "context_kind": "turn_capsule",
                    "content_id": record.id,
                    "fingerprint": fingerprint,
                    "coverage": {
                        "start_seq": int(metadata.get("start_seq", 0) or 0),
                        "end_seq": int(metadata.get("end_seq", 0) or 0),
                    },
                    "trust": "mixed_provenance",
                },
            )
        )
    return projected


def prepare_context_messages(
    conversation: Conversation,
    app_config: Any,
    *,
    default_work_dir: str = "",
    memory_prompt: str = "",
    include_environment: bool = True,
    captured_at: datetime | None = None,
    prompt_limit: int = 0,
    visible_tool_names: set[str] | None = None,
) -> List[Message]:
    return build_context_messages(
        conversation=conversation,
        app_config=app_config,
        default_work_dir=default_work_dir,
        memory_prompt=memory_prompt,
        include_environment=include_environment,
        captured_at=captured_at,
        prompt_limit=prompt_limit,
        visible_tool_names=visible_tool_names,
    )


def prepare_api_messages(
    messages: list[Message],
    *,
    conversation: Conversation | None,
    replay_pressure: str = "normal",
    visible_tool_names: set[str] | None = None,
) -> tuple[list[Message], Any]:
    """Apply request replay planning and return the tool-result renderer."""
    prepared = copy.deepcopy(messages)
    planner = ToolReplayPlanner(
        conversation=conversation,
        replay_pressure=replay_pressure,
        visible_tool_names=visible_tool_names,
    )
    planner.prepare_messages(prepared)
    return prepared, planner.render_tool_result
