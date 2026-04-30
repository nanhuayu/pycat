"""Agent execution service.

Provides helpers for building execution policies and running
context compression — extracted from MessagePresenter and MainWindow.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from models.conversation import Conversation
from models.provider import Provider

logger = logging.getLogger(__name__)


class AgentService:
    """Stateless helpers for agent execution lifecycle."""

    @staticmethod
    def build_run_policy(
        *,
        conversation: Conversation,
        app_settings: dict,
        mode_slug: str | None = None,
        enable_thinking: bool | None = None,
        enable_search: bool | None = None,
        enable_mcp: bool | None = None,
        work_dir: str | None = None,
    ):
        """Build a RunPolicy from conversation settings + app config.

        Returns a RunPolicy or raises on failure.
        """
        from core.task.builder import build_run_policy
        from core.modes.manager import ModeManager

        slug = mode_slug or str(getattr(conversation, "mode", "chat") or "chat")

        if enable_thinking is None:
            show_thinking_default = bool(app_settings.get("show_thinking", True))
            enable_thinking = bool(
                (conversation.settings or {}).get("show_thinking", show_thinking_default)
            )

        retry_config = None
        try:
            from core.config.schema import RetryConfig
            raw_retry = app_settings.get("retry")
            if raw_retry and isinstance(raw_retry, dict):
                retry_config = RetryConfig.from_dict(raw_retry)
        except Exception as e:
            logger.debug("Failed to load retry config: %s", e)

        wd = work_dir or getattr(conversation, "work_dir", None) or None
        mm = ModeManager(wd)

        return build_run_policy(
            mode_slug=slug,
            enable_thinking=bool(enable_thinking),
            enable_search=enable_search,
            enable_mcp=enable_mcp,
            mode_manager=mm,
            retry_config=retry_config,
        )

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
        """Run synchronous context condensation.

        Returns True on success, raises on failure.
        """
        from core.context.condenser import ContextCondenser
        condenser = ContextCondenser(client)
        state = conversation.get_state() if hasattr(conversation, "get_state") else None
        condenser.condense_state(conversation, provider, state)
        return True
