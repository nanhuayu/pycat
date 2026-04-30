"""Application-level dependency injection container.

Centralizes creation and wiring of all services and core components,
replacing implicit singleton patterns with explicit ownership.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.app import AppBootstrap, AppCoordinator
from services.storage_service import StorageService
from services.app_settings_service import AppSettingsService
from services.provider_catalog_service import ProviderCatalogService
from services.provider_service import ProviderService
from services.conversation_service import ConversationService
from services.command_service import CommandService
from services.context_service import ContextService
from services.skill_service import SkillService
from core.config import load_app_config
from core.llm.client import LLMClient
from core.prompts.assembler import PromptAssembler
from core.runtime.turn_engine import TurnEngine
from core.channel.runtime import ChannelRuntimeService
from core.tools.manager import ToolManager
from core.commands import CommandRegistry


@dataclass(frozen=True)
class AppServices:
    """Runtime services exposed to the UI and presenters."""

    storage: StorageService
    app_settings_service: AppSettingsService
    provider_catalog_service: ProviderCatalogService
    provider_service: ProviderService
    conv_service: ConversationService
    command_service: CommandService
    context_service: ContextService
    skill_service: SkillService
    command_registry: CommandRegistry
    tool_manager: ToolManager
    prompt_assembler: PromptAssembler
    client: LLMClient
    turn_engine: TurnEngine
    channel_runtime: ChannelRuntimeService
    app_coordinator: AppCoordinator
    app_bootstrap: AppBootstrap


class AppContainer:
    """Single owner of all application-level dependencies.

    Every component is created once and wired together here.
    UI code should access components via this container rather than
    instantiating services or managers directly.
    """

    def __init__(self) -> None:
        # -- Data & persistence
        storage = StorageService()
        self.app_config = load_app_config()

        # -- Core components (shared single instances)
        tool_manager = ToolManager()
        prompt_assembler = PromptAssembler()
        client = LLMClient(
            timeout=float(getattr(self.app_config, "llm_timeout_seconds", 600.0) or 600.0),
            tool_manager=tool_manager,
            prompt_assembler=prompt_assembler,
        )
        turn_engine = TurnEngine(client=client, tool_manager=tool_manager)

        # -- Service layer
        app_settings_service = AppSettingsService(storage)
        provider_service = ProviderService()
        provider_catalog_service = ProviderCatalogService(
            storage=storage,
            provider_service=provider_service,
        )
        conv_service = ConversationService(storage)
        app_coordinator = AppCoordinator(conv_service=conv_service)
        channel_runtime = ChannelRuntimeService(
            storage=storage,
            provider_catalog_service=provider_catalog_service,
            conv_service=conv_service,
            turn_engine=turn_engine,
        )
        app_bootstrap = AppBootstrap(
            app_settings_service=app_settings_service,
            provider_catalog_service=provider_catalog_service,
            conv_service=conv_service,
        )
        command_service = CommandService()
        context_service = ContextService(client)
        skill_service = SkillService()
        command_registry = CommandRegistry()

        self.services = AppServices(
            storage=storage,
            app_settings_service=app_settings_service,
            provider_catalog_service=provider_catalog_service,
            provider_service=provider_service,
            conv_service=conv_service,
            command_service=command_service,
            context_service=context_service,
            skill_service=skill_service,
            command_registry=command_registry,
            tool_manager=tool_manager,
            prompt_assembler=prompt_assembler,
            client=client,
            turn_engine=turn_engine,
            channel_runtime=channel_runtime,
            app_coordinator=app_coordinator,
            app_bootstrap=app_bootstrap,
        )
