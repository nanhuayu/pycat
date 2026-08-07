from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Callable

from core.agent.run.engine import AgentRunEngine
from core.agent.run.control import RunControl
from core.context.maintainer import ContextMaintainer
from core.content.session_content import SessionContentService
from core.capabilities.executor import CapabilityExecutor
from models.contracts.config import AppConfig
from core.prompts.renderer import PromptRenderer
from core.tools.base import ToolApprovalRequest
from core.tools.manager import ToolManager
from models.contracts.agent import RunPolicy, RunResult
from models.conversation import Conversation
from models.provider import Provider


class AgentRuntime:
    """Public facade for executing one Agent run."""

    def __init__(
        self,
        *,
        client: Any,
        tool_manager: ToolManager,
        prompt_renderer: PromptRenderer,
        capability_executor: CapabilityExecutor,
        app_config: AppConfig | None = None,
        provider_catalog_provider: Callable[[], Iterable[Provider]] | None = None,
        context_maintenance: ContextMaintainer | None = None,
        content_service: SessionContentService | None = None,
    ) -> None:
        self.client = client
        self.tool_manager = tool_manager
        self.prompt_renderer = prompt_renderer
        self.capability_executor = capability_executor
        self._provider_catalog_provider = provider_catalog_provider
        self._context_maintenance = context_maintenance
        self._content_service = content_service
        self._app_config = app_config or AppConfig()
        self._rebuild_turn_loop()

    def _rebuild_turn_loop(self) -> None:
        self._turn_loop = AgentRunEngine(
            client=self.client,
            tool_manager=self.tool_manager,
            prompt_renderer=self.prompt_renderer,
            capability_executor=self.capability_executor,
            app_config=self._app_config,
            provider_catalog_provider=self._provider_catalog_provider,
            context_maintenance=self._context_maintenance,
            content_service=self._content_service,
        )

    def update_configuration(self, app_config: AppConfig) -> None:
        self._app_config = app_config
        if self._context_maintenance is not None:
            self._context_maintenance.update_configuration(app_config)
        self._rebuild_turn_loop()

    async def run(
        self,
        *,
        provider: Provider,
        conversation: Conversation,
        policy: RunPolicy,
        on_event: Callable | None = None,
        on_token=None,
        on_thinking=None,
        approval_callback: Callable[[ToolApprovalRequest], Any] | None = None,
        questions_callback=None,
        cancel_event=None,
        debug_log_path: str | None = None,
        debug_trace=None,
        initial_runtime_messages=None,
        run_control: RunControl | None = None,
    ) -> RunResult:
        return await self._turn_loop.run(
            provider=provider,
            conversation=conversation,
            policy=policy,
            on_event=on_event,
            on_token=on_token,
            on_thinking=on_thinking,
            approval_callback=approval_callback,
            questions_callback=questions_callback,
            cancel_event=cancel_event,
            debug_log_path=debug_log_path,
            debug_trace=debug_trace,
            initial_runtime_messages=initial_runtime_messages,
            run_control=run_control,
        )
