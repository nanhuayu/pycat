"""Application-level dependency injection container.

Centralizes creation and wiring of all services and core components,
replacing implicit singleton patterns with explicit ownership.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from pycat.core.agent.run.runtime import AgentRuntime
from pycat.core.agent.tooling.executor import ToolExecutor
from pycat.core.app.bootstrap import AppBootstrap
from pycat.core.app.channel_platforms import build_channel_platforms
from pycat.core.app.coordinator import AppCoordinator
from pycat.core.app.loop_host import ApplicationLoop
from pycat.core.app.repositories import AppRepositories
from pycat.core.app.repositories.provider_credentials import ProviderCredentialsRepository
from pycat.core.app.services.app_settings import AppSettingsService
from pycat.core.app.services.channel import ChannelService
from pycat.core.app.services.codex_auth import CodexAuthService
from pycat.core.app.services.commands import CommandService
from pycat.core.app.services.context import ContextService
from pycat.core.app.services.conversation import ConversationService
from pycat.core.app.services.delegation import DelegationService
from pycat.core.app.services.extensions import ExtensionService
from pycat.core.app.services.interactive import InteractiveService
from pycat.core.app.services.knowledge import KnowledgeService
from pycat.core.app.services.mode_catalog import ModeCatalogService
from pycat.core.app.services.provider import ProviderService
from pycat.core.app.services.provider_catalog import ProviderCatalogService
from pycat.core.app.services.release import ReleaseChecker
from pycat.core.app.services.run import RunService
from pycat.core.app.services.search import SearchService
from pycat.core.app.services.settings_update import SettingsUpdateService
from pycat.core.app.services.skill import SkillService
from pycat.core.app.services.tools import McpService, ToolService
from pycat.core.app.services.workbench import WorkbenchService
from pycat.core.app.services.workbuddy_auth import WorkBuddyAuthService
from pycat.core.app.services.workspace import WorkspaceService
from pycat.core.capabilities import (
    CapabilitiesConfig,
    CapabilitiesManager,
    CapabilityExecutor,
    default_capabilities_config,
)
from pycat.core.channel.catalog import ChannelCatalog
from pycat.core.channel.gateway import ChannelGateway
from pycat.core.commands import CommandRegistry
from pycat.core.config.io import load_settings_dict
from pycat.core.content.ocr import OcrService
from pycat.core.content.resolver import SessionContentResolver
from pycat.core.content.session_content import SessionContentService
from pycat.core.content.wiki import WikiService
from pycat.core.llm.client import LLMClient
from pycat.core.memory.review import MemoryReviewService
from pycat.core.memory.worker import CurationWorker
from pycat.core.persistence import DataDirectoryLease
from pycat.core.prompts.renderer import PromptRenderer
from pycat.core.tools.manager import ToolManager
from pycat.models.contracts.config import AppConfig
from pycat.models.model_ref import split_model_ref


@dataclass(frozen=True)
class AppServices:
    """Runtime services exposed to the UI and presenters."""

    data_dir: Path
    app_settings_service: AppSettingsService
    release_checker: ReleaseChecker
    provider_catalog_service: ProviderCatalogService
    provider_service: ProviderService
    mode_catalog_service: ModeCatalogService
    conv_service: ConversationService
    context_service: ContextService
    content_service: SessionContentService
    workspace_service: WorkspaceService
    ocr_service: OcrService
    skill_service: SkillService
    extension_service: ExtensionService
    knowledge_service: KnowledgeService
    curation_worker: CurationWorker
    command_registry: CommandRegistry
    command_service: CommandService
    workbench: WorkbenchService
    interactive: InteractiveService
    tool_manager: ToolManager
    capability_executor: CapabilityExecutor
    agent_runtime: AgentRuntime
    run_service: RunService
    delegation_service: DelegationService
    tools: ToolService
    mcp: McpService
    channel_gateway: ChannelGateway
    channel_service: ChannelService
    channel_catalog: ChannelCatalog
    settings_update_service: SettingsUpdateService
    app_coordinator: AppCoordinator
    app_bootstrap: AppBootstrap


class AppContainer:
    """Single owner of all application-level dependencies.

    Every component is created once and wired together here.
    UI code should access components via this container rather than
    instantiating services or managers directly.
    """

    def __init__(
        self,
        *,
        data_dir: str | Path | None = None,
        background_curation: bool = True,
        transport_factory=None,
        background_loop: bool = False,
    ) -> None:
        root = Path(data_dir).expanduser().resolve() if data_dir is not None else Path.home() / ".pycat"
        self._lease = DataDirectoryLease(root)
        self._closed = False
        self._closing_task = None
        self._loop_host = None
        try:
            self._compose(root, transport_factory=transport_factory)
            if background_loop:
                self._loop_host = ApplicationLoop()

                async def bind():
                    self.services.run_service.bind_loop()

                self._loop_host.submit(bind()).result()
            if background_curation:
                self.services.curation_worker.start(
                    {str(row.get("work_dir") or "") for row in self.services.conv_service.list_all()}
                )
        except BaseException:
            if self._loop_host is not None:
                self._loop_host.close()
            self._lease.close()
            raise

    def _compose(self, data_dir: Path, *, transport_factory=None) -> None:
        repositories = AppRepositories.open(data_dir)
        self.app_config = AppConfig.from_dict(load_settings_dict(data_dir=data_dir))

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
        release_checker = ReleaseChecker()
        credentials = ProviderCredentialsRepository(data_dir)
        codex_auth = CodexAuthService(credentials, transport_factory=transport_factory)
        workbuddy_auth = WorkBuddyAuthService(credentials, transport_factory=transport_factory)
        provider_service = ProviderService(transport_factory=transport_factory, codex_auth=codex_auth, workbuddy_auth=workbuddy_auth)
        provider_catalog_service = ProviderCatalogService(
            repository=repositories.providers,
            provider_service=provider_service,
            model_reference_provider=model_references,
        )
        provider_catalog_service.load()
        mode_catalog_service = ModeCatalogService(data_dir=str(data_dir))

        search_service = SearchService(repositories.search_config.load())
        workspace_service = WorkspaceService(data_dir)
        content_service = SessionContentService(data_dir=str(data_dir), workspace_service=workspace_service)
        resolver = SessionContentResolver(content_service)
        wiki_service = WikiService(resolver)
        skill_service = SkillService(data_dir=str(data_dir))
        capabilities = CapabilitiesManager.merge(
            default_capabilities_config(),
            getattr(self.app_config, "capabilities", None) or CapabilitiesConfig(),
        )
        prompt_renderer = PromptRenderer(app_config=self.app_config)
        client = LLMClient(
            timeout=float(getattr(self.app_config, "llm_timeout_seconds", 600.0) or 600.0),
            transport_factory=transport_factory,
            headers_resolver=provider_service.request_headers,
        )
        self._prompt_renderer = prompt_renderer
        self._client = client
        capability_executor = CapabilityExecutor(
            client=client,
            prompt_renderer=prompt_renderer,
            capabilities=capabilities,
            provider_catalog_provider=provider_catalog_service.current,
            default_auxiliary_model=self.app_config.default_auxiliary_model,
            data_dir=str(data_dir),
        )
        ocr_service = OcrService(self.app_config.ocr, capability_executor=capability_executor)
        tool_manager = ToolManager(
            mcp_servers=repositories.mcp_servers,
            search_config=repositories.search_config,
            search_service=search_service,
            capabilities=capabilities,
            ocr_service=ocr_service,
            wiki_service=wiki_service,
            skill_service=skill_service,
            workspace_service=workspace_service,
        )
        context_service = ContextService(
            client,
            app_config=self.app_config,
            capability_executor=capability_executor,
        )
        conv_service = ConversationService(repositories.conversations)
        app_coordinator = AppCoordinator(conv_service=conv_service, active_processes=tool_manager.list_processes)

        def resolve_review_provider(source):
            provider_id = str(source.get("provider_id") or "")
            provider = next((item for item in provider_catalog_service.current() if item.id == provider_id), None)
            if provider is None:
                raise ValueError("记忆整理的服务商已移除或未配置，请检查模型设置后重试。")
            return provider

        curation_worker = CurationWorker(
            MemoryReviewService(capability_executor, skill_service, wiki_service=wiki_service, data_dir=str(data_dir)),
            conversation_loader=conv_service.load,
            provider_resolver=resolve_review_provider,
            settings_provider=repositories.settings.load,
            on_change=app_coordinator.invalidate_content,
            data_dir=str(data_dir),
        )
        knowledge_service = KnowledgeService(
            wiki_service=wiki_service,
            worker=curation_worker,
            capability_executor=capability_executor,
            resolver=resolver,
            on_change=app_coordinator.invalidate_content,
            data_dir=str(data_dir),
        )
        agent_runtime = AgentRuntime(
            client=client,
            tool_manager=tool_manager,
            prompt_renderer=prompt_renderer,
            capability_executor=capability_executor,
            app_config=self.app_config,
            provider_catalog_provider=provider_catalog_service.current,
            context_maintenance=context_service.maintenance,
            content_service=content_service,
            memory_worker=curation_worker,
        )
        capability_executor.bind_agent_runtime(agent_runtime)
        run_service = RunService(
            conversations=conv_service,
            models=provider_catalog_service,
            settings=app_settings_service,
            content=content_service,
            runtime=agent_runtime,
            data_dir=data_dir,
            context=context_service,
        )
        delegation_service = DelegationService(runs=run_service, tools=tool_manager, on_change=app_coordinator.invalidate_content)
        run_service.delegation = delegation_service
        tool_manager.registry.get_tool('agent__task').operation = delegation_service.operate
        channel_platforms = build_channel_platforms(content_resolver=resolver)
        channel_gateway = ChannelGateway(
            data_dir=repositories.data_dir,
            channel_catalog=channel_platforms.catalog,
            platform_backends=channel_platforms.backends,
            provider_catalog_service=provider_catalog_service,
            conv_service=conv_service,
            run_service=run_service,
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
        settings_update_service = SettingsUpdateService(
            app_settings_service=app_settings_service,
            provider_catalog_service=provider_catalog_service,
            mode_catalog_service=mode_catalog_service,
            repositories=repositories,
            channel_gateway=channel_gateway,
            channel_service=channel_service,
            runtime_config_applier=self.apply_runtime_configuration,
        )
        command_registry = CommandRegistry(data_dir=str(data_dir))
        tools_service = ToolService(
            manager=tool_manager,
            runs=run_service,
            executor=ToolExecutor(
                tool_manager,
                capability_executor=capability_executor,
                content_service=content_service,
                shell_config=self.app_config.shell,
            ),
        )
        mcp_service = McpService(manager=tool_manager, settings=settings_update_service, runs=run_service)
        extension_service = ExtensionService(data_dir=data_dir, skills=skill_service,
                                             mcp_probe=tool_manager.probe_server_connection)
        command_service = CommandService(registry=command_registry, runs=run_service,
            tools=tools_service, modes=mode_catalog_service)
        workbench = WorkbenchService(commands=command_service, settings=settings_update_service,
            providers=provider_catalog_service, provider_service=provider_service, modes=mode_catalog_service,
            tools=tools_service, mcp=mcp_service, skills=skill_service, knowledge=knowledge_service,
            workspace=workspace_service, channels=channel_service, content=content_service, release=release_checker, ocr=ocr_service,
            extensions=extension_service)
        command_registry.mention_provider = workbench.mention_candidates
        command_registry.argument_provider = workbench.argument_candidates
        interactive = InteractiveService(commands=command_service, workbench=workbench)

        self.services = AppServices(
            workspace_service=workspace_service,
            ocr_service=ocr_service,
            data_dir=repositories.data_dir,
            app_settings_service=app_settings_service,
            release_checker=release_checker,
            provider_catalog_service=provider_catalog_service,
            provider_service=provider_service,
            mode_catalog_service=mode_catalog_service,
            conv_service=conv_service,
            context_service=context_service,
            content_service=content_service,
            skill_service=skill_service,
            extension_service=extension_service,
            knowledge_service=knowledge_service,
            curation_worker=curation_worker,
            command_registry=command_registry,
            command_service=command_service,
            workbench=workbench,
            interactive=interactive,
            tool_manager=tool_manager,
            capability_executor=capability_executor,
            agent_runtime=agent_runtime,
            channel_gateway=channel_gateway,
            run_service=run_service,
            tools=tools_service,
            delegation_service=delegation_service,
            mcp=mcp_service,
            channel_service=channel_service,
            channel_catalog=channel_platforms.catalog,
            settings_update_service=settings_update_service,
            app_coordinator=app_coordinator,
            app_bootstrap=app_bootstrap,
        )

    def close(self) -> None:
        if self._closed and self._closing_task is not None and self._closing_task.done():
            self._closing_task.result()
            return
        if self._loop_host is not None:
            try:
                self._loop_host.submit(self.aclose()).result(timeout=30)
            finally:
                self._loop_host.close()
        else:
            asyncio.run(self.aclose())

    async def aclose(self) -> None:
        if self._closing_task is None:
            self._closing_task = asyncio.create_task(self._shutdown(), name="pycat-close")
        await asyncio.shield(self._closing_task)

    async def _shutdown(self) -> None:
        self._closed = True
        self.services.provider_service.close()
        try:
            await self.services.interactive.aclose()
            await self.services.delegation_service.aclose()
            await self.services.run_service.aclose()
            results = await asyncio.gather(
                asyncio.to_thread(self.services.curation_worker.close),
                asyncio.to_thread(self.services.channel_gateway.stop),
                return_exceptions=True,
            )
            await self.services.tool_manager.shutdown()
            await asyncio.to_thread(self.services.workspace_service.close)
            failures = [result for result in results if isinstance(result, BaseException)]
            if failures:
                raise RuntimeError("Application shutdown failed: " + "; ".join(map(str, failures)))
        finally:
            self._lease.close()

    def apply_runtime_configuration(self, settings: dict) -> AppConfig:
        """Apply persisted settings to all long-lived runtime services."""

        config = AppConfig.from_dict(settings)
        self.app_config = config
        capabilities = CapabilitiesManager.merge(
            default_capabilities_config(),
            config.capabilities,
        )
        services = self.services
        self._prompt_renderer.app_config = config
        services.capability_executor.update_configuration(
            capabilities=capabilities,
            default_auxiliary_model=config.default_auxiliary_model,
        )
        services.tool_manager.refresh_capability_tools(capabilities)
        services.agent_runtime.update_configuration(config)
        services.context_service.update_configuration(config)
        services.tool_manager.update_ocr_configuration(config.ocr)
        self._client.set_timeout(float(config.llm_timeout_seconds or 600.0))
        services.tool_manager.refresh_search_config()
        return config
