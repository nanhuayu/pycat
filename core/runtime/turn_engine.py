from __future__ import annotations

from typing import Callable, Optional

from models.conversation import Conversation
from models.provider import Provider

from core.runtime.events import TurnEvent
from core.task.task import Task
from core.task.types import RunPolicy, TaskEvent, TaskResult


class TurnEngine:
    """Runtime-facing execution facade over the task loop."""

    def __init__(self, *, task: Task | None = None, client=None, tool_manager=None) -> None:
        if task is not None:
            self._task = task
        else:
            self._task = Task(client=client, tool_manager=tool_manager)

    def add_pre_turn_hook(self, hook: Callable) -> None:
        self._task.add_pre_turn_hook(hook)

    def add_post_turn_hook(self, hook: Callable) -> None:
        self._task.add_post_turn_hook(hook)

    async def run(
        self,
        *,
        provider: Provider,
        conversation: Conversation,
        policy: RunPolicy,
        on_event: Optional[Callable[[TurnEvent], None]] = None,
        on_token=None,
        on_thinking=None,
        approval_callback=None,
        questions_callback=None,
        cancel_event=None,
        debug_log_path: Optional[str] = None,
    ) -> TaskResult:
        def _on_task_event(event: TaskEvent) -> None:
            if on_event is None:
                return
            on_event(TurnEvent.from_task_event(event))

        return await self._task.run(
            provider=provider,
            conversation=conversation,
            policy=policy,
            on_event=_on_task_event if on_event is not None else None,
            on_token=on_token,
            on_thinking=on_thinking,
            approval_callback=approval_callback,
            questions_callback=questions_callback,
            cancel_event=cancel_event,
            debug_log_path=debug_log_path,
        )
