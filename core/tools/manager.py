"""Unified tool manager with persistent conversation-scoped MCP sessions."""

import logging
import sys
import os
import asyncio
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
    ClientSession = object
    StdioServerParameters = object

from models.mcp_server import McpServerConfig
from services.storage_service import StorageService
from services.search_service import SearchService

from core.tools.registry import ToolRegistry
from core.tools.catalog import ToolAvailabilityContext, ToolDescriptor, ToolSelectionPolicy
from core.tools.mcp.proxies import McpProxyTool
from core.tools.mcp.naming import MCP_TOOL_PUBLIC_PREFIX, build_mcp_tool_name, is_mcp_tool_name, parse_mcp_tool_name
from core.tools.system.search import FetchUrlTool, WebSearchTool

# System Tools
from core.tools.system.filesystem import LsTool, ReadFileTool, GrepTool
from core.tools.system.python_exec import PythonExecTool
from core.tools.system.file_ops import WriteToFileTool, EditFileTool, DeleteFileTool
from core.tools.system.shell_exec import (
    ExecuteCommandTool,
    ShellStartTool,
    ShellStatusTool,
    ShellLogsTool,
    ShellWaitTool,
    ShellKillTool,
)
from core.tools.system.patch import PatchTool
from core.tools.system.state_mgr import StateMgrTool
from core.tools.system.multi_agent import (
    SubagentExploreTool,
    SubagentReadAnalyzeTool,
    SubagentSearchTool,
    SubagentCustomTool,
    AttemptCompletionTool,
    SwitchModeTool,
)
from core.tools.system.artifact_tools import ManageArtifactTool
from core.tools.system.todo_tools import ManageTodoTool
from core.tools.system.memory_tools import ManageMemoryTool
from core.tools.system.ask_questions import AskQuestionsTool
from core.tools.system.skills import LoadSkillTool, ReadSkillResourceTool
from core.tools.system.capability_tools import CAPABILITY_TOOL_PREFIX, build_capability_tools
from core.config.schema import ToolPermissionConfig, ToolPolicy

logger = logging.getLogger(__name__)


@dataclass
class _PersistentMcpSession:
    signature: str
    queue: Any
    task: Any

    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        await self.queue.put(("call", tool_name, arguments, future))
        return await future

    async def close(self) -> None:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        await self.queue.put(("close", None, None, future))
        await future
        await self.task

class ToolManager:
    """Unified tool manager.

    Owns built-in tools, search tools, and MCP-backed tools.
    Lifecycle is managed by ``AppContainer`` — do not instantiate directly
    outside of the container or ``LLMClient`` fallback path.
    """

    def __init__(self):
        self.storage = StorageService()
        self.servers: List[McpServerConfig] = []
        
        # Initialize Registry
        self.registry = ToolRegistry()
        
        # Register System Tools
        self._register_default_system_tools()
        
        # Search Service
        self.search_service = SearchService(self.storage.load_search_config())
        self.registry.register(WebSearchTool(self.search_service))
        
        # Helper for legacy
        self._workspace_root = Path(os.getcwd()).resolve()
        self._mcp_schema_cache: Dict[str, Tuple[str, List[Dict[str, Any]]]] = {}
        self._persistent_sessions: Dict[Tuple[str, str], _PersistentMcpSession] = {}
        self._mcp_loop: Optional[asyncio.AbstractEventLoop] = None
        self._mcp_loop_thread: Optional[threading.Thread] = None
        self._mcp_loop_lock = threading.Lock()
        self._mcp_loop_ready = threading.Event()

    def _register_default_system_tools(self):
        tools = [
            LsTool(), ReadFileTool(), GrepTool(), FetchUrlTool(),
            PythonExecTool(),
            WriteToFileTool(), EditFileTool(), DeleteFileTool(),
            ExecuteCommandTool(),
            ShellStartTool(), ShellStatusTool(), ShellLogsTool(), ShellWaitTool(), ShellKillTool(),
            PatchTool(),
            StateMgrTool(),
            ManageMemoryTool(),
            ManageTodoTool(),
            AskQuestionsTool(),
            ManageArtifactTool(),
            LoadSkillTool(), ReadSkillResourceTool(),
            SubagentExploreTool(), SubagentReadAnalyzeTool(), SubagentSearchTool(), SubagentCustomTool(),
            AttemptCompletionTool(), SwitchModeTool(),
        ]
        for tool in tools:
            self.registry.register(tool)
        # Register one independent tool per enabled capability (e.g. capability__translate)
        self._refresh_capability_tools()

    def _refresh_capability_tools(self) -> None:
        """Register dynamic capability tools with the ``capability__`` prefix."""
        self.registry.unregister_prefix(CAPABILITY_TOOL_PREFIX)
        for tool in build_capability_tools():
            self.registry.register(tool)

    def refresh_capability_tools(self) -> None:
        """Re-register capability tools after configuration changes."""
        self._refresh_capability_tools()

    def update_permissions(self, config: Dict[str, Any]):
        """Update permission settings in Registry."""
        self.registry.update_permissions(config)

    def refresh_search_config(self):
        """Reload search configuration from storage.
        
        Called after settings dialog saves new search configuration.
        """
        try:
            new_config = self.storage.load_search_config()
            self.search_service.update_config(new_config)
            logger.info("Search config refreshed: provider=%s", new_config.provider)
        except Exception as e:
            logger.warning("Failed to refresh search config: %s", e)

    async def get_all_tools(
        self,
        tool_selection: Optional[ToolSelectionPolicy] = None,
        availability_context: Optional[ToolAvailabilityContext] = None,
        tool_permissions: Optional[ToolPermissionConfig] = None,
    ) -> List[Dict[str, Any]]:
        """
        Refreshes tools in the registry (if needed) and returns schemas.

        Tool visibility is selected by ``tool_selection`` and effective permissions
        are resolved from ``tool_permissions``.
        """
        if tool_selection is None:
            tool_selection = ToolSelectionPolicy.all()
        if availability_context is None:
            availability_context = ToolAvailabilityContext(
                search_available=self.search_service.is_available(),
                mcp_available=MCP_AVAILABLE,
            )

        # Capability tools come from settings and may change while the app is running.
        self._refresh_capability_tools()

        # 1. Search Tool is always registered so it participates in the same
        # registry/permission model as other tools. Request-time capability and
        # provider availability only decide whether it is exposed to the model.
        self.registry.register(WebSearchTool(self.search_service, list(tool_selection.prepared_queries) or None))

        # 2. MCP Tools
        wants_mcp = tool_selection.allowed_categories is None or "mcp" in tool_selection.allowed_categories
        if wants_mcp and MCP_AVAILABLE:
            await self._run_on_mcp_loop(self._refresh_mcp_tools_impl())

        normalized_permissions = tool_permissions or ToolPermissionConfig()

        # 3. Return schemas from Registry with all filters applied
        descriptors = self.list_tool_descriptors(availability=availability_context)
        selected_tools = {
            name for name, descriptor in descriptors.items()
            if tool_selection.allows(descriptor)
        }

        all_schemas = self.registry.get_all_tool_schemas(
            tool_selection=None,
            tool_permissions=normalized_permissions,
        )

        filtered_schemas: List[Dict[str, Any]] = []
        for schema in all_schemas:
            fn = schema.get("function") or {}
            name = fn.get("name")
            if not isinstance(name, str) or not name:
                continue

            descriptor = descriptors.get(name)
            if name not in selected_tools or descriptor is None:
                continue
            if isinstance(fn, dict):
                fn["x_pycat_category"] = descriptor.category
                fn["x_pycat_source"] = descriptor.source
            filtered_schemas.append(schema)

        return filtered_schemas

    def list_tool_descriptors(
        self,
        *,
        availability: Optional[ToolAvailabilityContext] = None,
        include_dynamic: bool = True,
    ) -> Dict[str, ToolDescriptor]:
        """Return catalog descriptors for built-in, capability, search, and cached MCP tools."""
        if include_dynamic:
            self._refresh_capability_tools()
        if availability is None:
            availability = ToolAvailabilityContext(
                search_available=self.search_service.is_available(),
                mcp_available=MCP_AVAILABLE,
            )

        descriptors: Dict[str, ToolDescriptor] = {}
        for tool in self.registry.list_tools():
            source = getattr(tool, "source", "builtin")
            available = True
            if tool.name == "web_search":
                available = bool(availability.search_available)
                source = "search"
            elif is_mcp_tool_name(tool.name):
                available = bool(availability.mcp_available)
                source = "mcp"
            descriptor = ToolDescriptor.from_tool(tool, source=source, available=available)
            descriptors[descriptor.name] = descriptor

        if include_dynamic:
            for srv in self.storage.load_mcp_servers():
                if not srv.enabled:
                    continue
                for tool_name in srv.cached_tools or []:
                    full_name = build_mcp_tool_name(srv.name, tool_name)
                    descriptors.setdefault(
                        full_name,
                        ToolDescriptor(
                            name=full_name,
                            display_name=str(tool_name),
                            description=f"MCP tool from server {srv.name}.",
                            category="mcp",
                            source="mcp",
                            available=bool(availability.mcp_available),
                            metadata={"server": srv.name},
                        ),
                    )
        return descriptors

    async def _refresh_mcp_tools_impl(self):
        """Register MCP tool proxies using cached schemas where possible."""
        self.servers = self.storage.load_mcp_servers()
        self.registry.unregister_prefix(MCP_TOOL_PUBLIC_PREFIX)
        active_servers: Dict[str, str] = {}
        
        for config in self.servers:
            if not config.enabled:
                continue
            signature = self._config_signature(config)
            active_servers[config.name] = signature
            
            try:
                cached = self._mcp_schema_cache.get(config.name)
                if cached and cached[0] == signature:
                    schemas = cached[1]
                else:
                    schemas = await self._list_server_tools(config)
                    self._mcp_schema_cache[config.name] = (signature, schemas)

                # Cache discovered tool names back to server config
                discovered_names = [str(s.get("name", "")) for s in schemas if s.get("name")]
                if discovered_names != list(config.cached_tools or []):
                    config.cached_tools = discovered_names
                    self.storage.save_mcp_servers(self.servers)

                for schema in schemas:
                    proxy = McpProxyTool(self, config, schema["name"], schema)
                    self.registry.register(proxy)
            except Exception as e:
                logger.warning("Error listing tools from %s: %s", config.name, e)

        stale_cache_keys = [name for name in self._mcp_schema_cache.keys() if name not in active_servers]
        for name in stale_cache_keys:
            self._mcp_schema_cache.pop(name, None)

        stale_session_keys = [
            key for key, handle in self._persistent_sessions.items()
            if key[1] not in active_servers or handle.signature != active_servers.get(key[1])
        ]
        for key in stale_session_keys:
            await self._close_persistent_session_impl(key)

    async def execute_tool_with_context(self, tool_name: str, arguments: dict, context):
        """
        Delegate execution to Registry.
        Note: Called by the unified runtime (e.g. MessageEngine via UI runtime).
        """
        return await self.registry.execute(tool_name, arguments, context)

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict,
        work_dir: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> Any:
        return await self._run_on_mcp_loop(
            self._call_tool_impl(
                tool_name,
                arguments,
                work_dir=work_dir,
                conversation_id=conversation_id,
            )
        )

    async def close_conversation_sessions(self, conversation_id: Optional[str]) -> None:
        conv_key = (conversation_id or "").strip()
        if not conv_key or not self._has_mcp_loop():
            return
        await self._run_on_mcp_loop(self._close_conversation_sessions_impl(conv_key))

    async def shutdown(self) -> None:
        if not self._has_mcp_loop():
            return
        await self._run_on_mcp_loop(self._shutdown_impl())
        loop = self._mcp_loop
        thread = self._mcp_loop_thread
        if loop and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._mcp_loop = None
        self._mcp_loop_thread = None
        self._mcp_loop_ready.clear()

    def _config_signature(self, config: McpServerConfig) -> str:
        payload = {
            "name": config.name,
            "command": config.command,
            "args": list(config.args or []),
            "env": dict(config.env or {}),
            "enabled": bool(config.enabled),
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _session_key(self, conversation_id: Optional[str], server_name: str) -> Tuple[str, str]:
        return ((conversation_id or "__global__").strip() or "__global__", server_name)

    def _build_server_params(self, config: McpServerConfig) -> Any:
        env = os.environ.copy()
        env.update(config.env)
        return StdioServerParameters(
            command=config.command,
            args=config.args,
            env=env,
        )

    async def _list_server_tools(self, config: McpServerConfig) -> List[Dict[str, Any]]:
        params = self._build_server_params(config)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()

        schemas: List[Dict[str, Any]] = []
        for tool in result.tools:
            schemas.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.inputSchema,
                }
            )
        return schemas

    async def _open_persistent_session(self, config: McpServerConfig) -> _PersistentMcpSession:
        queue: asyncio.Queue = asyncio.Queue()
        ready = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(self._persistent_session_worker(config, queue, ready))
        await ready
        return _PersistentMcpSession(
            signature=self._config_signature(config),
            queue=queue,
            task=task,
        )

    async def _get_or_create_persistent_session(
        self,
        config: McpServerConfig,
        *,
        conversation_id: Optional[str],
    ) -> Any:
        key = self._session_key(conversation_id, config.name)
        signature = self._config_signature(config)
        handle = self._persistent_sessions.get(key)
        if handle and handle.signature == signature:
            return handle
        if handle:
            await self._close_persistent_session_impl(key)
        handle = await self._open_persistent_session(config)
        self._persistent_sessions[key] = handle
        return handle

    async def _close_persistent_session_impl(self, key: Tuple[str, str]) -> None:
        handle = self._persistent_sessions.pop(key, None)
        if not handle:
            return
        try:
            await handle.close()
        except Exception as exc:
            logger.debug("Failed to close MCP session %s: %s", key, exc)

    async def _call_tool_impl(
        self,
        tool_name: str,
        arguments: dict,
        work_dir: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> Any:
        server_name, real_tool_name = parse_mcp_tool_name(tool_name)
        if not server_name or not real_tool_name:
            return f"Invalid MCP tool name: {tool_name}"

        config = next((c for c in self.servers if c.name == server_name), None)
        if not config:
            self.servers = self.storage.load_mcp_servers()
            config = next((c for c in self.servers if c.name == server_name), None)

        if not config:
            return f"Server {server_name} not found or disabled"

        session_key = self._session_key(conversation_id, server_name)
        for attempt_index in range(2):
            try:
                session = await self._get_or_create_persistent_session(
                    config,
                    conversation_id=conversation_id,
                )
                result = await session.call_tool(real_tool_name, arguments)
                return self._format_tool_result(result)
            except Exception as e:
                logger.warning(
                    "MCP tool call failed (%s -> %s, attempt %s): %s",
                    server_name,
                    real_tool_name,
                    attempt_index + 1,
                    e,
                )
                await self._close_persistent_session_impl(session_key)
                if attempt_index >= 1:
                    return f"MCP Execution Error: {e}"

    async def _close_conversation_sessions_impl(self, conv_key: str) -> None:
        keys = [key for key in self._persistent_sessions.keys() if key[0] == conv_key]
        for key in keys:
            await self._close_persistent_session_impl(key)

    async def _shutdown_impl(self) -> None:
        keys = list(self._persistent_sessions.keys())
        for key in keys:
            await self._close_persistent_session_impl(key)

    async def _persistent_session_worker(
        self,
        config: McpServerConfig,
        queue: asyncio.Queue,
        ready: asyncio.Future,
    ) -> None:
        params = self._build_server_params(config)
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    if not ready.done():
                        ready.set_result(True)

                    while True:
                        action, tool_name, arguments, future = await queue.get()
                        if action == "close":
                            if future is not None and not future.done():
                                future.set_result(True)
                            break

                        if action != "call":
                            if future is not None and not future.done():
                                future.set_exception(RuntimeError(f"Unsupported MCP action: {action}"))
                            continue

                        try:
                            result = await session.call_tool(tool_name, arguments or {})
                        except Exception as exc:
                            if future is not None and not future.done():
                                future.set_exception(exc)
                        else:
                            if future is not None and not future.done():
                                future.set_result(result)
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
            raise

    def _has_mcp_loop(self) -> bool:
        loop = self._mcp_loop
        return bool(loop and loop.is_running())

    def _ensure_mcp_loop(self) -> None:
        with self._mcp_loop_lock:
            if self._has_mcp_loop():
                return

            self._mcp_loop_ready.clear()
            thread = threading.Thread(target=self._run_mcp_loop, name="PyCat-MCP", daemon=True)
            self._mcp_loop_thread = thread
            thread.start()

        self._mcp_loop_ready.wait(timeout=5)
        if not self._has_mcp_loop():
            raise RuntimeError("Failed to start MCP event loop")

    def _run_mcp_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._mcp_loop = loop
        self._mcp_loop_ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception as exc:
                logger.debug("Failed to shutdown async generators on MCP loop exit: %s", exc)
            try:
                loop.run_until_complete(loop.shutdown_default_executor())
            except Exception as exc:
                logger.debug("Failed to shutdown default executor on MCP loop exit: %s", exc)
            loop.close()

    async def _run_on_mcp_loop(self, coro: Any) -> Any:
        self._ensure_mcp_loop()
        loop = self._mcp_loop
        if loop is None:
            raise RuntimeError("MCP event loop is unavailable")
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return await asyncio.wrap_future(future)

    @staticmethod
    def _format_tool_result(result: Any) -> str:
        text_content: List[str] = []
        for content in getattr(result, "content", []) or []:
            content_type = getattr(content, "type", "")
            if content_type == "text":
                text_content.append(getattr(content, "text", ""))
            elif content_type == "image":
                text_content.append(f"[Image: {getattr(content, 'mimeType', 'unknown')}]")
            elif content_type == "resource":
                text_content.append(f"[Resource: {getattr(content, 'uri', '')}]")
        return "\n".join([item for item in text_content if item])
