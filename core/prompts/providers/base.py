from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

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


def synthetic_context_message(content: str, *, kind: str) -> Message:
    return Message(
        role="user",
        content=str(content or ""),
        metadata={"context_kind": kind, "synthetic": True},
    )
