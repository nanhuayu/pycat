"""Context management service used by the UI layer."""
from __future__ import annotations

from typing import Any

from pycat.core.capabilities.compression import CapabilityCompressor
from pycat.core.context.maintainer import ContextMaintainer
from pycat.models.contracts.config import AppConfig
from pycat.models.conversation import Conversation
from pycat.models.provider import Provider


class ContextService:
    """High-level context operations used by presenters and MainWindow."""

    def __init__(
        self,
        client: Any,
        *,
        app_config: AppConfig,
        capability_executor: Any = None,
        maintenance: ContextMaintainer | None = None,
    ) -> None:
        self._client = client
        self._capability_executor = capability_executor
        self._app_config = app_config
        self._maintenance = maintenance or ContextMaintainer(
            client=self._client,
            app_config=self._app_config,
            compression_factory=self._compression_factory(),
        )

    @property
    def maintenance(self) -> ContextMaintainer:
        return self._maintenance

    def update_configuration(self, app_config: AppConfig) -> None:
        self._app_config = app_config
        self._maintenance.update_configuration(app_config)

    def _compression_factory(self):
        if self._capability_executor is None:
            return None

        def factory(*, provider: Provider, debug_trace: Any = None):
            return CapabilityCompressor(
                provider,
                capability_executor=self._capability_executor,
                debug_trace=debug_trace,
            )

        return factory

    async def compact_async(self, conversation: Conversation, provider: Provider):
        """Compact idle history while retaining the latest real user turn.

        The explicit action need not retain the automatic path's three-turn
        starting target. Closed tails may still use their recoverable capsules.
        """
        return await self._maintenance.maintain_async(
            conversation,
            provider=provider,
            client=self._client,
            current_seq=conversation.current_seq_id(),
            force=True,
            honor_auto_enabled=False,
            recent_turn_target=1,
            protect_current_turn=False,
        )
