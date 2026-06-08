"""Agent execution service.

Provides helpers for building execution policies and running
context compression — extracted from MessagePresenter and MainWindow.
"""
from __future__ import annotations

import logging
from typing import Any

from models.conversation import Conversation
from models.provider import Provider

logger = logging.getLogger(__name__)


class AgentService:
    """Stateless helpers for agent execution lifecycle."""

    @staticmethod
    def get_debug_log_path(app_settings: dict, storage) -> str | None:
        """Return the debug log path if stream logging is enabled."""
        if not bool(app_settings.get("log_stream", False)):
            return None
        try:
            return str(storage.data_dir / "stream_debug.log")
        except Exception as e:
            logger.debug("Failed to construct debug log path: %s", e)
            return None

    @staticmethod
    def compact_conversation(
        conversation: Conversation,
        provider: Provider,
        client: Any,
    ) -> bool:
        """Run deterministic context maintenance synchronously."""
        from core.context.maintenance import ContextMaintenanceService

        ContextMaintenanceService().maintain(
            conversation,
            context_window_limit=0,
            current_seq=conversation.current_seq_id(),
            force=True,
        )
        return True
