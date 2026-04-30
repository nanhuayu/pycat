"""Context management service.

Wraps ContextCondenser / ContextManager for use by the UI layer,
providing a simpler API and decoupling UI from core internals.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from models.conversation import Conversation, Message
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
        from core.context.condenser import ContextCondenser

        condenser = ContextCondenser(self._client)
        state = conversation.get_state() if hasattr(conversation, "get_state") else None
        condenser.condense_state(conversation, provider, state)
        return True

    async def auto_condense(
        self,
        conversation: Conversation,
        provider: Provider,
        context_window_limit: int,
        app_config: Any = None,
    ) -> None:
        """Run the full async condense pipeline (per-message + global archive)."""
        from core.context.condenser import ContextCondenser

        condenser = ContextCondenser(self._client)
        await condenser.auto_condense(
            conversation=conversation,
            provider=provider,
            context_window_limit=context_window_limit,
            app_config=app_config,
        )

    @staticmethod
    def estimate_tokens(conversation: Conversation) -> int:
        """Rough token estimate for the active messages in a conversation."""
        total = 0
        for msg in conversation.messages:
            if msg.condense_parent:
                continue
            content = msg.content or ""
            total += len(content) // 4 + 1
        return total

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
