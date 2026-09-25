"""Unified tool manager with persistent conversation-scoped MCP sessions."""

import asyncio
import hashlib
import json
import logging
import os
import sys
import threading
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from importlib import metadata
from importlib.util import find_spec
from typing import Any, Dict, List, Optional, Tuple

from pycat.core.capabilities.tool_adapter import CAPABILITY_TOOL_PREFIX, build_capability_tools
from pycat.core.content.ocr import OcrService, OcrStatus
from pycat.core.tools.base import ToolResult
from pycat.core.tools.mcp.browser import (
    prepare_browser_daemon,
    reap_browser_processes,
    scoped_arguments,
    scoped_config,
    scoped_schema,
)
from pycat.core.tools.mcp.naming import (
    MCP_TOOL_PUBLIC_PREFIX,
    build_mcp_tool_name,
    is_mcp_tool_name,
    parse_mcp_tool_name,
)
from pycat.core.tools.mcp.proxies import McpProxyTool
from pycat.core.tools.process import BackgroundProcessManager
from pycat.core.tools.registry import ToolRegistry
from pycat.core.tools.system.artifact_tools import ManageArtifactTool
from pycat.core.tools.system.ask_questions import AskQuestionsTool
from pycat.core.tools.system.content_tools import ArchiveListTool, ArchiveReadTool
from pycat.core.tools.system.file_ops import DeleteFileTool, EditFileTool, WriteToFileTool

# System Tools
from pycat.core.tools.system.filesystem import DeliverFilesTool, GrepTool, LsTool, ReadFileTool
from pycat.core.tools.system.memory_tools import ManageMemoryTool
from pycat.core.tools.system.multi_agent import AgentCompleteTool, AgentRunTool, AgentTaskTool
from pycat.core.tools.system.ocr import FileOcrTool
from pycat.core.tools.system.patch import PatchTool
from pycat.core.tools.system.python_exec import PythonExecTool
from pycat.core.tools.system.search import FetchUrlTool, WebSearchTool
from pycat.core.tools.system.shell_exec import (
    ExecuteCommandTool,
    ShellKillTool,
    ShellListTool,
    ShellReadTool,
    ShellWriteTool,
)
from pycat.core.tools.system.skill_manage import ManageSkillTool
from pycat.core.tools.system.skills import LoadSkillTool, ReadSkillResourceTool
from pycat.core.tools.system.todo_tools import ManageTodoTool
from pycat.core.tools.system.wiki_tools import ManageWikiTool
from pycat.models.contracts.capability import CapabilitiesConfig
from pycat.models.contracts.config import OcrConfig
from pycat.models.contracts.mcp import (
    TRANSPORT_SSE,
    TRANSPORT_STDIO,
    TRANSPORT_STREAMABLE_HTTP,
    McpServerConfig,
)
from pycat.models.contracts.tooling import (
    ToolAvailabilityContext,
    ToolDescriptor,
    ToolPermissionConfig,
    ToolSelectionPolicy,
)
from pycat.models.session_paths import has_active_workspace

logger = logging.getLogger(__name__)

# The catalog needs package availability, not an initialized protocol client.
# SDK imports and errors belong to the connection operation below.
MCP_AVAILABLE = find_spec("mcp") is not None
MCP_TRANSPORTS_AVAILABLE = {
    "stdio": MCP_AVAILABLE,
    "streamable_http": MCP_AVAILABLE,
    "sse": MCP_AVAILABLE,
}
MCP_CLOSE_TIMEOUT_SECONDS = 5.0
MCP_THREAD_JOIN_TIMEOUT_SECONDS = 1.0
MCP_TEST_CONNECTION_TIMEOUT_SECONDS = 60.0
_WORKSPACE_OR_FULL_ACCESS_TOOLS = {
    "file__write",
    "file__edit",
    "file__patch",
    "file__delete",
    "python__exec",
    "shell__run",
    "shell__read",
    "shell__list",
    "shell__kill",
    "shell__write",
}


def _safe_transport_name(server: Any) -> str:
    try:
        return str(server.normalized_transport() or "")
    except (AttributeError, TypeError, ValueError):
        return ""


def _transport_available(transport: str) -> bool:
    """Report configured transport support; opening it verifies the SDK/runtime."""
    return MCP_AVAILABLE and MCP_TRANSPORTS_AVAILABLE.get(str(transport or ""), False)


def _any_transport_available() -> bool:
    return any(_transport_available(name) for name in MCP_TRANSPORTS_AVAILABLE)


@dataclass
class _PersistentMcpSession:
    signature: str
    queue: Any
    task: Any

    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        return await self._request("call", tool_name, arguments)

    async def _request(self, action, tool_name=None, arguments=None):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        await self.queue.put((action, tool_name, arguments, future))
        try:
            await asyncio.wait((future, self.task), return_when=asyncio.FIRST_COMPLETED)
            if future.done():
                return future.result()
            if not self.task.cancelled():
                self.task.result()
            raise RuntimeError("MCP session closed before the queued request completed")
        finally:
            if not future.done():
                future.cancel()

    async def close(self) -> None:
        if not self.task.done():
            await self._request("close")
        if not self.task.cancelled():
            await self.task

class ToolManager:
    """Unified tool manager.

    Owns built-in tools, search tools, and MCP-backed tools.
    Lifecycle is managed by ``AppContainer``. Tests that need a manager should
    use an explicit fake storage/search service factory.
    """

    def __init__(
        self,
        *,
        mcp_servers: Any,
        search_config: Any,
        search_service: Any,
        capabilities: CapabilitiesConfig | None = None,
        ocr_service: OcrService | None = None,
        wiki_service: Any = None,
        skill_service: Any = None,
        workspace_service: Any = None,
    ):
        self.mcp_servers = mcp_servers
        self.search_config = search_config
        self.servers: List[McpServerConfig] = []
        self.capabilities = capabilities or CapabilitiesConfig()
        self._wiki_service = wiki_service
        self._skill_service = skill_service
        self.workspace_service = workspace_service
        self.ocr_service = ocr_service or OcrService(OcrConfig(enabled=False))
        self._ocr_tool = FileOcrTool(self.ocr_service)
        
        # Initialize Registry
        self.registry = ToolRegistry()
        
        # Register System Tools
        self._register_default_system_tools()
        
        self.search_service = search_service
        self.registry.register(WebSearchTool(self.search_service))
        
        self._mcp_schema_cache: Dict[str, Tuple[str, List[Dict[str, Any]]]] = {}
        self._persistent_sessions: Dict[Tuple[str, str], _PersistentMcpSession] = {}
        self._session_open_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._browser_namespace = "pc-" + uuid.uuid4().hex[:16]
        self._mcp_loop: Optional[asyncio.AbstractEventLoop] = None
        self._mcp_loop_thread: Optional[threading.Thread] = None
        self._mcp_loop_lock = threading.Lock()
        self._mcp_loop_ready = threading.Event()

        # Single owner of every background shell process (shell__run/read/list/kill).
        self.processes = BackgroundProcessManager()

    def _register_default_system_tools(self):
        tools = [
            LsTool(), ReadFileTool(), self._ocr_tool, GrepTool(), DeliverFilesTool(), FetchUrlTool(),
            PythonExecTool(),
            WriteToFileTool(), EditFileTool(), DeleteFileTool(),
            ExecuteCommandTool(),
            ShellReadTool(), ShellKillTool(), ShellListTool(), ShellWriteTool(),
            PatchTool(),
            ArchiveListTool(), ArchiveReadTool(),
            ManageMemoryTool(),
            ManageWikiTool(self._wiki_service),
            ManageTodoTool(),
            AskQuestionsTool(),
            ManageArtifactTool(),
            LoadSkillTool(), ReadSkillResourceTool(), ManageSkillTool(self._skill_service),
            AgentRunTool(), AgentCompleteTool(), AgentTaskTool(),
        ]
        for tool in tools:
            self.registry.register(tool)
        # Register one independent tool per agent-visible capability (e.g. capability__translate)
        self._refresh_capability_tools()

    def _refresh_capability_tools(self) -> None:
        """Register dynamic capability tools with the ``capability__`` prefix."""
        self.registry.replace_prefix(
            CAPABILITY_TOOL_PREFIX,
            build_capability_tools(self.capabilities),
        )

    def refresh_capability_tools(self, capabilities: CapabilitiesConfig | None = None) -> None:
        """Re-register capability tools after configuration changes."""
        if capabilities is not None:
            self.capabilities = capabilities
        self._refresh_capability_tools()

    def update_ocr_configuration(self, config: OcrConfig) -> None:
        self.ocr_service.update_configuration(config)

    def ocr_status(self) -> OcrStatus:
        return self.ocr_service.status()

    def refresh_search_config(self):
        """Reload search configuration from storage.
        
        Called after settings dialog saves new search configuration.
        """
        try:
            new_config = self.search_config.load()
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

        # MCP schemas are refreshed only when the current request can expose MCP.
        wants_mcp = tool_selection.allowed_categories is None or "mcp" in tool_selection.allowed_categories
        if wants_mcp and _any_transport_available():
            await self._run_on_mcp_loop(self._refresh_mcp_tools_impl())

        normalized_permissions = tool_permissions or ToolPermissionConfig()

        # Return schemas from the current immutable registry snapshot.
        descriptors = self.list_tool_descriptors(availability=availability_context)
        selected_tools = {
            name for name, descriptor in descriptors.items()
            if tool_selection.allows(descriptor)
            or (
                name == "agent__complete"
                and availability_context.completion_policy == "explicit"
            )
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
                if name == "agent__run":
                    fn["parameters"] = AgentRunTool.input_schema_for_work_dir(
                        availability_context.work_dir, data_dir=availability_context.data_dir or None
                    )
                fn["x_pycat_category"] = descriptor.category
                fn["x_pycat_source"] = descriptor.source
                fn["x_pycat_risk"] = descriptor.risk
            filtered_schemas.append(schema)

        return filtered_schemas

    def list_tool_descriptors(
        self,
        *,
        availability: Optional[ToolAvailabilityContext] = None,
        include_dynamic: bool = True,
    ) -> Dict[str, ToolDescriptor]:
        """Return a read-only catalog snapshot without refreshing shared tools."""
        if availability is None:
            availability = ToolAvailabilityContext(
                search_available=self.search_service.is_available(),
                mcp_available=MCP_AVAILABLE,
            )

        descriptors: Dict[str, ToolDescriptor] = {}
        for tool in self.registry.list_tools():
            source = getattr(tool, "source", "builtin")
            available = True
            if (
                tool.name in _WORKSPACE_OR_FULL_ACCESS_TOOLS
                and not has_active_workspace(availability.work_dir)
                and availability.filesystem_mode != "full_access"
            ):
                available = False
            elif tool.name == "web__search":
                available = bool(availability.search_available)
                source = "search"
            elif tool.name == "file__deliver":
                available = bool(
                    has_active_workspace(availability.work_dir)
                    and (str(availability.source or "desktop") in {"desktop", "cli", "sdk"}
                         or (availability.source == 'channel' and availability.channel_file_delivery))
                )
            elif tool.name == "file__ocr":
                try:
                    available = bool(self.ocr_service.is_available())
                except Exception:
                    available = False
            elif tool.name == "agent__complete" and availability.completion_policy:
                available = availability.completion_policy == "explicit"
            elif tool.name == "state__memory":
                # Visibility follows the same conversation policy as the
                # service-level write gate.  Execution still re-checks it.
                available = bool(availability.memory_enabled)
            elif is_mcp_tool_name(tool.name):
                transport = ""
                config = getattr(tool, "config", None)
                try:
                    transport = config.normalized_transport() if config is not None else ""
                except (TypeError, ValueError):
                    transport = ""
                available = bool(
                    availability.mcp_available
                    and _transport_available(transport)
                )
                source = "mcp"
            descriptor = ToolDescriptor.from_tool(tool, source=source, available=available)
            descriptors[descriptor.name] = descriptor

        if include_dynamic:
            for srv in self.mcp_servers.load():
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
                            available=bool(
                                availability.mcp_available
                                and _transport_available(_safe_transport_name(srv))
                            ),
                            risk="high",
                            metadata={"server": srv.name},
                        ),
                    )
        return descriptors

    async def _refresh_mcp_tools_impl(self):
        """Register MCP tool proxies using cached schemas where possible."""
        self.servers = self.mcp_servers.load()
        discovered_tools: list[McpProxyTool] = []
        active_servers: Dict[str, str] = {}
        
        for config in self.servers:
            if not config.enabled:
                continue
            transport = _safe_transport_name(config)
            if not _transport_available(transport):
                logger.warning(
                    "Skipping MCP server %s: transport %r is unavailable",
                    config.name,
                    transport or "unknown",
                )
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
                    self.mcp_servers.save(self.servers)

                for schema in schemas:
                    discovered_tools.append(McpProxyTool(self, config, schema["name"], schema))
            except Exception as e:
                logger.warning("Error listing tools from %s: %s", config.name, e)

        stale_cache_keys = [name for name in self._mcp_schema_cache.keys() if name not in active_servers]
        for name in stale_cache_keys:
            self._mcp_schema_cache.pop(name, None)
        self.registry.replace_prefix(MCP_TOOL_PUBLIC_PREFIX, discovered_tools)

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

    def test_server_connection(self, config: McpServerConfig) -> Dict[str, Any]:
        """Run one real discovery round for ``config`` on the MCP loop.

        Synchronous wrapper intended for the settings UI: performs
        ``initialize()`` + ``list_tools()`` against the candidate config and
        returns ``{"ok": bool, "tool_count": int, "tools": [...], "error": str}``.
        Failures are reported, never raised, so the dialog can render them.
        """
        future = asyncio.run_coroutine_threadsafe(self.probe_server_connection(config), self._ensure_mcp_loop_and_get())
        try:
            return future.result(timeout=MCP_TEST_CONNECTION_TIMEOUT_SECONDS + 1)
        except Exception as exc:
            future.cancel()
            return {"ok": False, "tool_count": 0, "tools": [], "error": str(exc or type(exc).__name__)}

    async def probe_server_connection(self, config: McpServerConfig) -> Dict[str, Any]:
        async def probe():
            schemas = await self._list_server_tools(config)
            names = [str(s["name"]) for s in schemas if s.get("name")]
            return {"ok": True, "tool_count": len(names), "tools": names, "schemas": schemas, "error": ""}
        try:
            return await asyncio.wait_for(self._run_on_mcp_loop(probe()), timeout=MCP_TEST_CONNECTION_TIMEOUT_SECONDS)
        except Exception as exc:
            return {"ok": False, "tool_count": 0, "tools": [], "error": str(exc or type(exc).__name__)}

    def _ensure_mcp_loop_and_get(self) -> asyncio.AbstractEventLoop:
        self._ensure_mcp_loop()
        loop = self._mcp_loop
        if loop is None:
            raise RuntimeError("MCP event loop is unavailable")
        return loop

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict,
        work_dir: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> ToolResult:
        return await self._run_on_mcp_loop(
            self._call_tool_impl(
                tool_name,
                arguments,
                work_dir=work_dir,
                conversation_id=conversation_id,
            )
        )

    def list_processes(self, conversation_id: str, *, include_exited: bool = False) -> list:
        """Read-only snapshot of one conversation's shell processes."""
        conv_key = (conversation_id or "").strip()
        if not conv_key:
            return []
        return self.processes.list(conversation_id=conv_key, include_exited=include_exited)

    def stop_process(self, process_id: str, *, conversation_id: str) -> bool:
        """Stop one shell process owned by the conversation; False when unknown."""
        try:
            self.processes.kill(process_id, conversation_id=(conversation_id or "").strip())
            return True
        except KeyError:
            return False
        except Exception as exc:
            logger.debug("Failed to stop shell process %s: %s", process_id, exc)
            return False

    def stop_all_processes(self, conversation_id: str) -> int:
        """Stop every unfinished shell process owned by the conversation."""
        conv_key = (conversation_id or "").strip()
        if not conv_key:
            return 0
        return self.processes.kill_conversation(conv_key)

    async def close_conversation_sessions(self, conversation_id: Optional[str]) -> bool:
        conv_key = (conversation_id or "").strip()
        if not conv_key:
            return True
        # Shell processes first (their logs live under the session root that the
        # caller is about to delete), then this conversation's MCP sessions.
        try:
            self.processes.kill_conversation(conv_key)
        except Exception as exc:
            logger.debug("Failed to kill conversation shell processes for %s: %s", conv_key, exc)
        if not self._has_mcp_loop():
            return True
        try:
            await asyncio.wait_for(
                self._run_on_mcp_loop(self._close_conversation_sessions_impl(conv_key)),
                timeout=MCP_CLOSE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Timed out closing MCP sessions for conversation %s after %.1fs",
                conv_key,
                MCP_CLOSE_TIMEOUT_SECONDS,
            )
            return False
        except Exception as exc:
            logger.debug("Failed to close MCP sessions for %s: %s", conv_key, exc)
            return False
        return True

    async def shutdown(self) -> None:
        # Shutdown order: kill background Shell processes first, then close MCP.
        try:
            self.processes.kill_all()
        except Exception as exc:
            logger.debug("Failed to kill background shell processes on shutdown: %s", exc)
        if not self._has_mcp_loop():
            return
        try:
            await asyncio.wait_for(
                self._run_on_mcp_loop(self._shutdown_impl()),
                timeout=MCP_CLOSE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Timed out shutting down MCP sessions after %.1fs",
                MCP_CLOSE_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.debug("Failed to shutdown MCP sessions: %s", exc)
        finally:
            # A stuck server must not keep the application close path alive.
            # The MCP loop is an owner-local daemon thread; stopping it here
            # also abandons any late completion futures after a timeout.
            self._stop_mcp_loop()

    def _stop_mcp_loop(self) -> None:
        loop = self._mcp_loop
        thread = self._mcp_loop_thread
        if loop and loop.is_running():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=MCP_THREAD_JOIN_TIMEOUT_SECONDS)
        self._mcp_loop = None
        self._mcp_loop_thread = None
        self._mcp_loop_ready.clear()

    def _config_signature(self, config: McpServerConfig) -> str:
        payload = {
            "name": config.name,
            "transport": config.normalized_transport(),
            "command": config.command,
            "args": list(config.args or []),
            "env": dict(config.env or {}),
            "cwd": config.cwd,
            "url": config.url,
            "headers": dict(config.headers or {}),
            "enabled": bool(config.enabled),
            "integration": config.integration,
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _session_key(self, conversation_id: Optional[str], server_name: str) -> Tuple[str, str]:
        return ((conversation_id or "__global__").strip() or "__global__", server_name)

    def _build_server_params(self, config: McpServerConfig) -> Any:
        from mcp import StdioServerParameters

        env = os.environ.copy()
        if config.integration == "agent-browser":
            env = {key: value for key, value in env.items() if not key.startswith("AGENT_BROWSER_")}
        env.update(config.env)
        cwd = str(config.cwd or "").strip()
        return StdioServerParameters(
            command=config.command,
            args=config.args,
            env=env,
            cwd=cwd or None,
        )

    @asynccontextmanager
    async def _client_context(self, config: McpServerConfig):
        """Own one MCP 2 client; transport and client exit in their entering task."""
        transport = config.normalized_transport()
        if not MCP_AVAILABLE:
            raise RuntimeError("MCP core dependency is unavailable")
        if not _transport_available(transport):
            raise RuntimeError(f"MCP transport {transport!r} is unavailable")
        try:
            sdk_version = metadata.version('mcp')
        except metadata.PackageNotFoundError:
            sdk_version = '未安装'
        try:
            major, minor = (int(part) for part in sdk_version.split('.')[:2])
            supported = major == 2 and minor >= 2
        except ValueError:
            supported = False
        if getattr(sys, 'frozen', False) or '__compiled__' in globals():
            repair = '请重新安装完整的 PyCat 发行目录并重启。'
        else:
            prefix = '& ' if os.name == 'nt' else ''
            repair = f'请在当前 Python 环境执行：\n{prefix}"{sys.executable}" -m pip install --upgrade "mcp>=2.2,<3"\n完成后重启 PyCat。'
        if not supported:
            raise RuntimeError(f'MCP 运行依赖版本为 {sdk_version}，需要 mcp>=2.2,<3。\n{repair}')
        try:
            from mcp import Client
        except ImportError as exc:
            raise RuntimeError(f'MCP {sdk_version} 安装不完整：{exc}\n{repair}') from exc

        headers = {str(k): str(v) for k, v in (config.headers or {}).items()}
        async with AsyncExitStack() as stack:
            if transport == TRANSPORT_STDIO:
                from mcp.client.stdio import stdio_client

                connection = stdio_client(self._build_server_params(config))
            elif transport == TRANSPORT_STREAMABLE_HTTP:
                import httpx2
                from mcp.client.streamable_http import streamable_http_client

                # MCP streams may remain open between messages; retain the SDK's
                # 30s connect/write and 300s stream-read timeout policy.
                http = await stack.enter_async_context(httpx2.AsyncClient(
                    headers=headers, timeout=httpx2.Timeout(30.0, read=300.0),
                ))
                connection = streamable_http_client(config.url, http_client=http)
            elif transport == TRANSPORT_SSE:
                from mcp.client.sse import sse_client

                connection = sse_client(config.url, headers=headers)
            else:
                raise ValueError(f"Unsupported MCP transport: {transport!r}")
            async with Client(connection, mode="legacy" if transport == TRANSPORT_SSE else "auto", cache=None) as client:
                yield client

    async def _list_server_tools(self, config: McpServerConfig) -> List[Dict[str, Any]]:
        tools = []
        cursors = set()
        async with self._client_context(config) as client:
            cursor = None
            while True:
                result = await client.list_tools(cursor=cursor)
                tools.extend(result.tools)
                cursor = result.next_cursor
                if cursor is None:
                    break
                if cursor in cursors:
                    raise RuntimeError("MCP server repeated a tools/list cursor")
                cursors.add(cursor)
        schemas: List[Dict[str, Any]] = []
        for tool in tools:
            annotations = tool.annotations.model_dump(by_alias=True, exclude_none=True) if tool.annotations else None
            schemas.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                    "annotations": annotations,
                }
            )
        if config.integration == "agent-browser":
            return [value for schema in schemas if (value := scoped_schema(schema)) is not None]
        return schemas

    async def _open_persistent_session(self, config: McpServerConfig, *, browser_session: str = "") -> _PersistentMcpSession:
        queue: asyncio.Queue = asyncio.Queue()
        ready = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(self._persistent_session_worker(config, queue, ready, browser_session=browser_session))
        try:
            await ready
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
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
        async with self._session_open_locks.setdefault(key, asyncio.Lock()):
            signature = self._config_signature(config)
            handle = self._persistent_sessions.get(key)
            if handle and handle.signature == signature:
                return handle
            if handle:
                await self._close_persistent_session_impl(key)
            if config.integration == "agent-browser":
                browser_session = hashlib.sha256("\0".join(key).encode()).hexdigest()[:16]
                handle = await self._open_persistent_session(config, browser_session=browser_session)
            else:
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
    ) -> ToolResult:
        server_name, real_tool_name = parse_mcp_tool_name(tool_name)
        if not server_name or not real_tool_name:
            return ToolResult(f"Invalid MCP tool name: {tool_name}", is_error=True)

        config = next((c for c in self.servers if c.name == server_name), None)
        if not config:
            self.servers = self.mcp_servers.load()
            config = next((c for c in self.servers if c.name == server_name), None)

        if not config or not config.enabled:
            return ToolResult(f"Server {server_name} not found or disabled", is_error=True)

        if config.integration == "agent-browser":
            try:
                scoped_arguments(real_tool_name, arguments, "", "")
            except ValueError as exc:
                return ToolResult(str(exc), is_error=True)

        session_key = self._session_key(conversation_id, server_name)
        session = None
        try:
            session = await self._get_or_create_persistent_session(config, conversation_id=conversation_id)
            result = await session.call_tool(real_tool_name, arguments)
            return self._format_tool_result(result)
        except asyncio.CancelledError:
            if config.integration == "agent-browser" and session is not None and self._persistent_sessions.get(session_key) is session:
                handle = self._persistent_sessions.pop(session_key, None)
                if handle:
                    handle.task.cancel()
                    await asyncio.gather(handle.task, return_exceptions=True)
            raise
        except Exception as exc:
            logger.warning("MCP tool call failed (%s -> %s): %s", server_name, real_tool_name, exc)
            if session is not None and self._persistent_sessions.get(session_key) is session:
                await self._close_persistent_session_impl(session_key)
            # A lost response does not prove the operation was not executed.
            # Reconnect on the next explicit call, never replay this one.
            return ToolResult(f"MCP Execution Error: {exc}", is_error=True)

    async def _close_conversation_sessions_impl(self, conv_key: str) -> None:
        keys = [key for key in self._persistent_sessions.keys() if key[0] == conv_key]
        for key in keys:
            await self._close_persistent_session_impl(key)
            self._session_open_locks.pop(key, None)

    async def _shutdown_impl(self) -> None:
        keys = list(self._persistent_sessions.keys())
        for key in keys:
            await self._close_persistent_session_impl(key)
        self._session_open_locks.clear()

    async def _persistent_session_worker(
        self,
        config: McpServerConfig,
        queue: asyncio.Queue,
        ready: asyncio.Future,
        *, browser_session: str = "",
    ) -> None:
        namespace = self._browser_namespace if browser_session else ""
        if browser_session:
            config = scoped_config(config, namespace, browser_session)
        try:
            async with self._client_context(config) as client:
                if not ready.done():
                    ready.set_result(True)
                try:
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
                        if future is not None and future.cancelled():
                            continue
                        try:
                            payload = scoped_arguments(tool_name, arguments or {}, namespace, browser_session) if browser_session else arguments or {}
                            if browser_session:
                                if tool_name not in {"agent_browser_close", "agent_browser_tools_profiles"}:
                                    await prepare_browser_daemon(config)
                                result = await asyncio.wait_for(client.call_tool(tool_name, payload), timeout=payload["timeoutMs"] / 1000 + 5)
                            else:
                                result = await client.call_tool(tool_name, payload)
                        except Exception as exc:
                            if future is not None and not future.done():
                                future.set_exception(exc)
                        else:
                            if future is not None and not future.done():
                                future.set_result(result)
                finally:
                    if browser_session:
                        try:
                            await asyncio.wait_for(client.call_tool("agent_browser_close", {
                                "namespace": namespace, "session": browser_session}), timeout=2)
                        except Exception as exc:
                            logger.debug("Browser cleanup failed for owned session %s: %s", browser_session, exc)
                        finally:
                            cleanup = asyncio.create_task(asyncio.to_thread(reap_browser_processes, config))
                            try:
                                await asyncio.shield(cleanup)
                            except asyncio.CancelledError:
                                await cleanup
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
    def _format_tool_result(result: Any) -> ToolResult:
        text_content: List[str] = []
        images: List[dict[str, Any]] = []
        for content in getattr(result, "content", []) or []:
            content_type = getattr(content, "type", "")
            if content_type == "text":
                text_content.append(getattr(content, "text", ""))
            elif content_type == 'image':
                images.append({'type': 'image', 'mimeType': content.mime_type, 'data': content.data})
            elif content_type == 'audio':
                text_content.append(f"[{content_type.title()}: {content.mime_type}]")
            elif content_type == "resource":
                text_content.append(f"[Resource: {content.resource.uri}]\n{getattr(content.resource, 'text', '')}")
            elif content_type == "resource_link":
                text_content.append(f"[Resource: {content.uri}]")
        structured = result.structured_content
        if structured is not None and not any(text_content):
            text_content.append(json.dumps(structured, ensure_ascii=False))
        text = "\n".join(item for item in text_content if item)
        content = [{'type': 'text', 'text': text}, *images] if images else text
        return ToolResult(content, is_error=result.is_error,
                          metadata={"structured_content": structured} if structured is not None else None)
