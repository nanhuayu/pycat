"""View-only projection that groups consecutive assistant steps into one run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, TypeAlias

from models.conversation import Message


@dataclass(frozen=True)
class SingleMessageItem:
    message: Message

    @property
    def messages(self) -> tuple[Message, ...]:
        return (self.message,)


@dataclass(frozen=True)
class AssistantRunGroup:
    messages: tuple[Message, ...]

    @property
    def primary_message(self) -> Message:
        return self.messages[-1]

    @property
    def process_messages(self) -> tuple[Message, ...]:
        return self.messages[:-1]

    @property
    def tool_call_count(self) -> int:
        return sum(len(message.tool_calls or []) for message in self.messages)


ConversationRenderItem: TypeAlias = SingleMessageItem | AssistantRunGroup


def project_message_runs(messages: Iterable[Message]) -> list[ConversationRenderItem]:
    """Group adjacent assistant messages without changing the persisted messages."""
    projected: list[ConversationRenderItem] = []
    assistant_buffer: list[Message] = []

    def flush_assistant_buffer() -> None:
        if not assistant_buffer:
            return
        if len(assistant_buffer) == 1:
            projected.append(SingleMessageItem(assistant_buffer[0]))
        else:
            projected.append(AssistantRunGroup(tuple(assistant_buffer)))
        assistant_buffer.clear()

    for message in messages or []:
        if str(getattr(message, "role", "") or "") == "assistant":
            assistant_buffer.append(message)
            continue
        flush_assistant_buffer()
        projected.append(SingleMessageItem(message))
    flush_assistant_buffer()
    return projected
