from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from core.context.items import ContextItem
from models.conversation import Conversation, Message


@dataclass(frozen=True)
class ProviderContext:
    conversation: Conversation
    app_config: Any
    work_dir: str
    latest_user_query: str = ""


class ContextProvider(Protocol):
    """Small synchronous provider interface for runtime context assembly."""

    name: str
    priority: int

    def build(self, context: ProviderContext) -> list[Message]:
        """Return synthetic context messages for this provider."""
        ...

    def build_items(self, context: ProviderContext) -> list[ContextItem]:
        """Return budget-aware context items for this provider."""
        ...


def synthetic_context_message(content: str, *, kind: str) -> Message:
    return Message(
        role="user",
        content=str(content or ""),
        metadata={"context_kind": kind, "synthetic": True},
    )


def context_item(content: str, *, kind: str, priority: int, item_id: str | None = None, required: bool = False, source_ref: str = "", metadata: dict[str, Any] | None = None) -> ContextItem:
    return ContextItem(
        id=item_id or kind,
        kind=kind,
        content=str(content or ""),
        priority=priority,
        required=required,
        source_ref=source_ref,
        metadata=dict(metadata or {}),
    )


class MessageProviderMixin:
    """Compatibility mixin: providers can expose ContextItems and Messages."""

    name: str
    priority: int

    def build_items(self, context: ProviderContext) -> list[ContextItem]:
        return []

    def build(self, context: ProviderContext) -> list[Message]:
        return [item.to_message() for item in self.build_items(context)]
