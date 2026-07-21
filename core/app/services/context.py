"""Context management service used by the UI layer."""
from __future__ import annotations

import asyncio
from typing import Any

from core.capabilities.compression import CapabilityCompressionOrchestrator
from core.context.maintainer import ContextMaintainer
from models.contracts.config import AppConfig
from models.conversation import Conversation
from models.provider import Provider

class ContextService:
    """High-level context operations used by presenters and MainWindow."""

    def __init__(self, client: Any, *, app_config: AppConfig, capability_executor: Any = None) -> None:
        self._client = client
        self._capability_executor = capability_executor
        self._app_config = app_config
        self._rebuild_maintenance()

    def _rebuild_maintenance(self) -> None:
        self._maintenance = ContextMaintainer(
            client=self._client,
            app_config=self._app_config,
            compressor_factory=self._compressor_factory(),
        )

    def update_configuration(self, app_config: AppConfig) -> None:
        self._app_config = app_config
        self._rebuild_maintenance()

    def _compressor_factory(self):
        if self._capability_executor is None:
            return None

        def factory(*, client: Any, provider: Provider, store: Any, debug_trace: Any = None):
            return CapabilityCompressionOrchestrator(
                client,
                provider,
                store=store,
                capability_executor=self._capability_executor,
                debug_trace=debug_trace,
            )

        return factory

    def compact(self, conversation: Conversation, provider: Provider) -> bool:
        """Compatibility wrapper for Qt slots that still call compact synchronously.

        Returns True on success.
        Raises on failure so callers can show an error message.
        """
        return asyncio.run(self.compact_async(conversation, provider))

    async def compact_async(self, conversation: Conversation, provider: Provider) -> bool:
        """Condense the conversation context through the async maintenance path."""
        await self._maintenance.maintain_async(
            conversation,
            provider=provider,
            client=self._client,
            context_window_limit=0,
            current_seq=conversation.current_seq_id(),
            force=True,
            honor_auto_enabled=False,
        )
        return True
