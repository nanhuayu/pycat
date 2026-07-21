"""Application-level dependency injection container.

Centralizes creation and wiring of all services and core components,
replacing implicit singleton patterns with explicit ownership.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.app import AppBootstrap, AppCoordinator
from core.app.services.app_settings import AppSettingsService
from core.app.services.channel import ChannelService
from core.app.services.provider_catalog import ProviderCatalogService
from core.app.services.provider import ProviderService
from core.app.services.mode_catalog import ModeCatalogService
from core.app.services.conversation import ConversationService
from core.app.services.context import ContextService
from core.app.services.skill import SkillService
from core.app.services.search import SearchService
from core.app.repositories import AppRepositories
from core.app.channel_platforms import build_channel_platforms
from core.config import load_app_config
from core.llm.client import LLMClient
from core.prompts.renderer import PromptRenderer
from core.agent.run.runtime import AgentRuntime
from core.capabilities import CapabilitiesConfig, CapabilitiesManager, CapabilityExecutor, default_capabilities_config
from core.channel.gateway import ChannelGateway
from core.channel.catalog import ChannelCatalog
from core.tools.manager import ToolManager
from core.commands import CommandRegistry
from models.contracts.config import AppConfig
from models.model_ref import split_model_ref


@dataclass(frozen=True)
class AppServices:
    """Runtime services exposed to the UI and presenters."""

    repositories: AppRepositories
    app_settings_service: AppSettingsService
    provider_catalog_service: ProviderCatalogService
    provider_service: ProviderService
    mode_catalog_service: ModeCatalogService
    conv_service: ConversationService
    context_service: ContextService
    skill_service: SkillService
    command_registry: CommandRegistry
    tool_manager: ToolManager
    prompt_renderer: PromptRenderer
    client: LLMClient
    capability_executor: CapabilityExecutor
    agent_runtime: AgentRuntime
    channel_gateway: ChannelGateway
    channel_service: ChannelService
    channel_catalog: ChannelCatalog
    app_coordinator: AppCoordinator
    app_bootstrap: AppBootstrap


class AppContainer:
    """Single owner of all application-level dependencies.

    Every component is created once and wired together here.
    UI code should access components via this container rather than
    instantiating services or managers directly.
    """

    def __init__(self) -> None:
        repositories = AppRepositories.open()
        self.app_config = load_app_config()

        def model_references():
            references = list(repositories.conversations.list_model_references())
            for model_ref in (
                self.app_config.default_chat_model,
                self.app_config.default_auxiliary_model,
            ):
                provider_name, model = split_model_ref(model_ref)
                if model:
                    references.append(
                        {
                            "provider_id": "",
                            "provider_name": provider_name,
                            "model": model,
                        }
                    )
            return references

        app_settings_service = AppSettingsService(repositories.settings)
        provider_service = ProviderService()
        provider_catalog_service = ProviderCatalogService(
            repository=repositories.providers,
            provider_service=provider_service,
            model_reference_provider=model_references,
        )
        provider_catalog_service.load()
        mode_catalog_service = ModeCatalogService()

        search_service = SearchService(repositories.search_config.load())
        capabilities = CapabilitiesManager.merge(
            default_capabilities_config(),
            getattr(self.app_config, "capabilities", None) or CapabilitiesConfig(),
        )
        tool_manager = ToolManager(
            mcp_servers=repositories.mcp_servers,
            search_config=repositories.search_config,
            search_service=search_service,
            capabilities=capabilities,
        )
        prompt_renderer = PromptRenderer(app_config=self.app_config)
        client = LLMClient(
            timeout=float(getattr(self.app_config, "llm_timeout_seconds", 600.0) or 600.0),
        )
        capability_executor = CapabilityExecutor(
            client=client,
            prompt_renderer=prompt_renderer,
            capabilities=capabilities,
            provider_catalog_provider=provider_catalog_service.current,
            default_auxiliary_model=self.app_config.default_auxiliary_model,
        )
        agent_runtime = AgentRuntime(
            client=client,
            tool_manager=tool_manager,
            prompt_renderer=prompt_renderer,
            capability_executor=capability_executor,
            app_config=self.app_config,
            provider_catalog_provider=provider_catalog_service.current,
        )
        capability_executor.bind_agent_runtime(agent_runtime)
        conv_service = ConversationService(
            repositories.conversations,
            workspace_sessions=repositories.workspace_sessions,
        )
        app_coordinator = AppCoordinator(conv_service=conv_service)
        channel_platforms = build_channel_platforms()
        channel_gateway = ChannelGateway(
            data_dir=repositories.data_dir,
            channel_catalog=channel_platforms.catalog,
            platform_backends=channel_platforms.backends,
            provider_catalog_service=provider_catalog_service,
            conv_service=conv_service,
            agent_runtime=agent_runtime,
            app_settings_provider=repositories.settings.load,
        )
        channel_service = ChannelService(
            channel_catalog=channel_platforms.catalog,
            channel_gateway=channel_gateway,
            wechat_login_flow=channel_platforms.wechat_login,
            configured_channels_provider=lambda: AppConfig.from_dict(repositories.settings.load()).channels,
        )
        app_bootstrap = AppBootstrap(
            app_settings_service=app_settings_service,
            provider_catalog_service=provider_catalog_service,
            conv_service=conv_service,
        )
        context_service = ContextService(client, app_config=self.app_config, capability_executor=capability_executor)
        skill_service = SkillService()
        command_registry = CommandRegistry()

        self.services = AppServices(
            repositories=repositories,
            app_settings_service=app_settings_service,
            provider_catalog_service=provider_catalog_service,
            provider_service=provider_service,
            mode_catalog_service=mode_catalog_service,
            conv_service=conv_service,
            context_service=context_service,
            skill_service=skill_service,
            command_registry=command_registry,
            tool_manager=tool_manager,
            prompt_renderer=prompt_renderer,
            client=client,
            capability_executor=capability_executor,
            agent_runtime=agent_runtime,
            channel_gateway=channel_gateway,
            channel_service=channel_service,
            channel_catalog=channel_platforms.catalog,
            app_coordinator=app_coordinator,
            app_bootstrap=app_bootstrap,
        )

    def apply_runtime_configuration(self, settings: dict) -> AppConfig:
        """Apply persisted settings to all long-lived runtime services."""

        config = AppConfig.from_dict(settings)
        self.app_config = config
        capabilities = CapabilitiesManager.merge(
            default_capabilities_config(),
            config.capabilities,
        )
        services = self.services
        services.prompt_renderer.app_config = config
        services.capability_executor.update_configuration(
            capabilities=capabilities,
            default_auxiliary_model=config.default_auxiliary_model,
        )
        services.tool_manager.refresh_capability_tools(capabilities)
        services.agent_runtime.update_configuration(config)
        services.context_service.update_configuration(config)
        services.client.set_timeout(float(config.llm_timeout_seconds or 600.0))
        return config
