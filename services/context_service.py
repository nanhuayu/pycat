"""Context management service used by the UI layer."""
from __future__ import annotations

import logging
from typing import Any

from core.llm.token_budget import estimate_conversation_tokens
from models.conversation import Conversation
from models.provider import Provider

logger = logging.getLogger(__name__)


class ContextService:
    """High-level context operations used by presenters and MainWindow."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def compact(self, conversation: Conversation, provider: Provider) -> bool:
        """Condense the conversation context synchronously.

        Returns True on success.
        Raises on failure so callers can show an error message.
        """
        from core.context.maintenance import ContextMaintenanceService

        ContextMaintenanceService().maintain(
            conversation,
            context_window_limit=0,
            current_seq=conversation.current_seq_id(),
            force=True,
        )
        return True

    async def auto_condense(
        self,
        conversation: Conversation,
        provider: Provider,
        context_window_limit: int,
        app_config: Any = None,
    ) -> None:
        """Run the full async context maintenance pipeline."""
        from core.context.maintenance import ContextMaintenanceService

        await ContextMaintenanceService().maintain_async(
            conversation,
            client=self._client,
            provider=provider,
            context_window_limit=context_window_limit,
            current_seq=conversation.current_seq_id(),
        )

    @staticmethod
    def estimate_tokens(conversation: Conversation) -> int:
        """Rough token estimate for the active messages in a conversation."""
        active = [msg for msg in conversation.messages if not msg.condense_parent]
        return estimate_conversation_tokens(active)

    @staticmethod
    def should_compress(
        conversation: Conversation,
        context_window_limit: int,
        *,
        max_active: int = 20,
        token_ratio: float = 0.7,
    ) -> bool:
        """Check whether compression should be triggered."""
        active = [m for m in conversation.messages if not m.condense_parent]
        if len(active) > max_active:
            return True
        tokens = ContextService.estimate_tokens(conversation)
        return tokens > context_window_limit * token_ratio
