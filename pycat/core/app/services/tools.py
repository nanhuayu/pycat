"""Application tool calls: policy, approval, execution and durable receipts."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from pathlib import Path

from pycat.core.agent.policy import RunPolicyBuilder
from pycat.core.agent.tooling.executor import ToolExecutor
from pycat.core.agent.tooling.result_recorder import ToolResultRecorder
from pycat.core.hosts.shell import available_shells
from pycat.core.llm.model_selection import resolve_provider_model_ref, select_default_provider_model
from pycat.core.tools.base import ToolResult
from pycat.core.tools.manager import MCP_AVAILABLE
from pycat.models.contracts.agent import ConversationBusyError, InvalidRequestError, PersistenceError, ToolCallResult
from pycat.models.contracts.tooling import ToolAvailabilityContext
from pycat.models.conversation import Message

_RUN_CONTROL_PREFIXES = ("agent__", "user__", "context__")


class ToolService:
    def __init__(self, *, manager, runs, executor: ToolExecutor):
        self._manager = manager
        self._runs = runs
        self._executor = executor
        self._recorder = ToolResultRecorder()

    def _conversation(self, conversation_id, work_dir):
        conversation = (
            self._runs.conversations.load(conversation_id) if conversation_id else self._runs.conversations.create()
        )
        if conversation is None:
            raise InvalidRequestError(f"Conversation not found: {conversation_id}")
        if not conversation_id:
            conversation.mode = "agent"
        if work_dir is not None:
            if conversation_id and work_dir != conversation.work_dir:
                raise InvalidRequestError("Change the workspace before calling a tool.")
            service = getattr(self._manager, "workspace_service", None)
            if service is not None:
                try:
                    conversation.work_dir = service.validate(work_dir)
                except (OSError, ValueError) as exc:
                    raise InvalidRequestError(str(exc)) from exc
            else:
                if work_dir and not Path(work_dir).expanduser().is_dir():
                    raise InvalidRequestError(f"Workspace not found: {work_dir}")
                conversation.work_dir = str(Path(work_dir).expanduser().resolve()) if work_dir else ""
        return conversation

    async def list(self, *, conversation_id=None, work_dir=None):
        self._runs.bind_loop()
        conversation = await asyncio.to_thread(self._conversation, conversation_id, work_dir)
        policy = RunPolicyBuilder.build(
            conversation=conversation, app_settings=self._runs.settings.load(), source="sdk"
        )
        schemas = await self._manager.get_all_tools(
            policy.tool_selection,
            availability_context=ToolAvailabilityContext(
                work_dir=conversation.work_dir,
                source="sdk",
                data_dir=conversation.data_dir,
                search_available=self._manager.search_service.is_available(),
                mcp_available=MCP_AVAILABLE,
                filesystem_mode=policy.filesystem_scope.mode,
            ),
            tool_permissions=policy.tool_permissions,
        )
        return [s for s in schemas if not s["function"]["name"].startswith(_RUN_CONTROL_PREFIXES)]

    async def call(self, name: str, arguments: dict, *, conversation_id=None, work_dir=None, approval_callback=None):
        return await self._call(
            name, arguments, conversation_id=conversation_id, work_dir=work_dir, approval_callback=approval_callback
        )

    async def run_shell(self, command: str, *, conversation_id: str, approval_callback=None):
        """Execute an explicit ! command with a durable user-visible receipt."""
        message = Message(
            role="user",
            content=f"!{command}",
            metadata={
                "command_run": {
                    "source_prefix": "!",
                    "action": "shell_run",
                    "command": command,
                }
            },
        )
        return await self._call(
            "shell__run",
            {"command": command, "cwd": "."},
            conversation_id=conversation_id,
            approval_callback=approval_callback,
            message=message,
        )

    async def _call(
        self, name, arguments, *, conversation_id=None, work_dir=None, approval_callback=None, message=None,
        terminal_controller="agent",
    ):
        self._runs.bind_loop()
        if name.startswith(_RUN_CONTROL_PREFIXES):
            raise InvalidRequestError("Run control tools require an active Agent run.")
        conversation = await asyncio.to_thread(self._conversation, conversation_id, work_dir)
        token = self._runs.conversations.begin_turn(conversation.id)
        if not token:
            raise ConversationBusyError(f"Conversation is busy: {conversation.id}")
        task = asyncio.current_task()
        self._runs._tasks.add(task)
        try:
            if conversation_id:
                conversation = self._runs.conversations.load(conversation_id)
                if conversation is None:
                    raise InvalidRequestError("Conversation no longer exists.")
            if message is not None:
                conversation.add_message(message)
            if not self._runs.conversations.save(conversation, activity_token=token):
                raise PersistenceError("Could not save tool context; execution was not started.")
            policy = RunPolicyBuilder.build(
                conversation=conversation, app_settings=self._runs.settings.load(), source="sdk"
            )
            # Refresh dynamic descriptors through their existing MCP owner.
            available = {s["function"]["name"] for s in await self.list(conversation_id=conversation.id)}
            tool = self._manager.registry.get_tool(name)
            if tool is None:
                raise InvalidRequestError(f"Tool not found: {name}")
            call_id = uuid.uuid4().hex
            provider = None
            if name.startswith("capability__"):
                selection = select_default_provider_model(
                    self._runs.models.current(),
                    default_model_ref=self._runs.settings.load().get("default_auxiliary_model", ""),
                )
                provider = selection.provider
                capability = getattr(tool, "capability", None)
                if capability is not None and capability.operation == "image":
                    provider = resolve_provider_model_ref(
                        [item for item in self._runs.models.current() if item.enabled],
                        capability.model_target.model_ref,
                    ).provider
                if provider is None:
                    raise InvalidRequestError("Configure a model before calling a capability tool.")
            context = self._executor.build_tool_context(
                conversation=conversation,
                provider=provider,
                approval_callback=approval_callback,
                questions_callback=None,
                llm_client=None,
                policy=policy,
                tool_call_id=call_id,
                tool_name=name,
            )
            context.runtime = replace(context.runtime, terminal_controller=terminal_controller)
            try:
                result = await self._executor.execute_tool(
                    tool_name=name,
                    tool_args=dict(arguments),
                    allowed=name in available and self._executor.is_tool_allowed(name, policy),
                    policy=policy,
                    context=context,
                )
            except asyncio.CancelledError:
                result = ToolResult("Tool call cancelled; inspect the workspace before retrying.", is_error=True)
            block = self._recorder.build_block(
                conversation=conversation,
                tool_name=name,
                tool_category=tool.category,
                tool_call_id=call_id,
                tool_args=arguments,
                result=result,
            )
            receipt = ToolCallResult(call_id, name, result.to_string(), result.is_error, block["result"], conversation)
            if message is not None:
                metadata = dict(message.metadata["command_run"], is_error=result.is_error)
                conversation.add_message(
                    Message(role="assistant", content=receipt.content, metadata={"command_run": metadata})
                )
            latest = self._runs.conversations.load(conversation.id)
            if latest is not None:
                conversation.settings = dict(latest.settings)
            if not self._runs.conversations.save(conversation, activity_token=token):
                raise PersistenceError("Tool finished but the receipt could not be saved.", result=receipt)
            return receipt
        finally:
            self._runs.conversations.end_turn(conversation.id, token)
            self._runs._tasks.discard(task)

    async def open_terminal(self, *, conversation_id: str, approval_callback=None):
        return await self._call("shell__run", {"interactive": True, "wait_seconds": 0},
                                conversation_id=conversation_id, approval_callback=approval_callback,
                                terminal_controller="user")

    def shell_choices(self):
        return available_shells()

    def processes(self, conversation_id: str, *, include_exited: bool = False):
        return self._manager.list_processes(conversation_id, include_exited=include_exited)

    def read_process(self, process_id: str, *, conversation_id: str, cursor: int = 0):
        return self._manager.processes.read(process_id, conversation_id=conversation_id, cursor=cursor,
            max_bytes=32768)

    def write_terminal(self, process_id: str, text: str, *, conversation_id: str):
        return self._manager.processes.write(process_id, text, conversation_id=conversation_id, controller="user")

    def terminal_response(self, process_id: str, text: str, *, conversation_id: str):
        return self._manager.processes.respond(process_id, text, conversation_id=conversation_id)

    def terminal_control(self, process_id: str, *, conversation_id: str, controller: str):
        return self._manager.processes.set_controller(process_id, controller, conversation_id=conversation_id)

    def resize_terminal(self, process_id: str, columns: int, rows: int, *, conversation_id: str):
        return self._manager.processes.resize(process_id, columns, rows, conversation_id=conversation_id)

    def stop_process(self, process_id: str, *, conversation_id: str):
        return self._manager.processes.kill(process_id, conversation_id=conversation_id)


class McpService:
    """MCP configuration and probes share the settings and connection owners."""

    def __init__(self, *, manager, settings, runs):
        self._manager, self._settings, self._runs = manager, settings, runs

    def list(self):
        return self._settings.load_snapshot().mcp_servers

    def save(self, servers):
        return self._settings.save_mcp(servers)

    async def probe(self, config):
        self._runs.bind_loop()
        return await asyncio.to_thread(self._manager.test_server_connection, config)
