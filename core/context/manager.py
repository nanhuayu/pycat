from __future__ import annotations

from typing import Any, List

from core.prompts.context_assembler import build_context_messages
from core.prompts.system_builder import build_system_prompt
from models.conversation import Conversation, Message
from models.provider import Provider


class ContextManager:
    """Assemble model messages after context maintenance has already run."""

    def __init__(self, *, keep_last_turns: int = 3) -> None:
        self.keep_last_turns = max(1, int(keep_last_turns or 3))

    async def prepare_messages(
        self,
        conversation: Conversation,
        provider: Provider,
        context_window_limit: int,
        tools: List[Any],
        app_config: Any,
        default_work_dir: str = ".",
    ) -> List[Message]:
        effective_messages = build_context_messages(
            conversation,
            app_config=app_config,
            keep_last_turns=self.keep_last_turns,
            default_work_dir=default_work_dir,
        )

        system_override = ""
        try:
            system_override = str(conversation.get_llm_config().system_prompt_override or "").strip()
        except Exception:
            settings = getattr(conversation, "settings", {}) or {}
            system_override = str(settings.get("system_prompt_override") or "").strip()

        if system_override:
            system_content = system_override
        else:
            system_content = build_system_prompt(
                conversation=conversation,
                tools=tools,
                provider=provider,
                app_config=app_config,
                default_work_dir=default_work_dir,
            )

        return [Message(role="system", content=system_content)] + effective_messages

    def reset(self) -> None:
        return None
