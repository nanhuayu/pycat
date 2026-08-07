"""Tool execution module - handles tool calls and state management.

Owns the unified selection and permission boundary around ToolManager calls.
"""
from __future__ import annotations

import json
import inspect
import logging
from dataclasses import replace
from typing import Any, Callable, Optional

from models.conversation import Conversation, Message
from models.provider import Provider

from core.state.operations import state_checkpoint
from core.tools.base import (
    PermissionContext,
    ToolApprovalRequest,
    ToolContext,
    ToolResult,
    ToolRuntimeContext,
)
from models.contracts.tooling import normalize_risk_level, normalize_tool_category
from core.tools.manager import ToolManager
from models.contracts.agent import RunPolicy

logger = logging.getLogger(__name__)


class ToolExecutor:
    """Handles tool execution and state management."""

    def __init__(
        self,
        tool_manager: ToolManager,
        *,
        capability_executor: Any = None,
        compression_factory: Any = None,
        shell_config: Any = None,
        content_service: Any = None,
    ):
        self._tool_manager = tool_manager
        self._capability_executor = capability_executor
        self._compression_factory = compression_factory
        self._shell_config = shell_config
        self._content_service = content_service

    def parse_tool_call(self, tool_call: dict) -> tuple[str, dict, Optional[str]]:
        """Parse tool call from LLM response.

        Args:
            tool_call: Tool call dict from LLM

        Returns:
            Tuple of (tool_name, args_dict, tool_call_id)
        """
        tool_name = tool_call.get("function", {}).get("name", "")
        args_str = tool_call.get("function", {}).get("arguments", "{}")
        tool_call_id = tool_call.get("id")

        try:
            args = json.loads(args_str) if isinstance(args_str, str) else {}
        except Exception:
            args = {}

        return str(tool_name), (args if isinstance(args, dict) else {}), tool_call_id

    def is_tool_allowed(self, tool_name: str, policy: RunPolicy) -> bool:
        """Check if tool is allowed by policy.

        Resolution order:
        1. Effective policy from ``policy.tool_permissions``.
        2. Tool selection policy category/tool/source filter.
        3. Default: allowed.
        """
        try:
            tool = self._tool_manager.registry.get_tool(tool_name)
            if tool is None:
                return False

            # 1. Permission policy
            category = str(getattr(tool, "category", "capability") or "capability")
            tool_policy = policy.tool_permissions.resolve(tool_name, category)
            if tool_policy.action == "deny":
                return False

            # 2. Request-time tool selection filter
            descriptors = self._tool_manager.list_tool_descriptors(include_dynamic=False)
            descriptor = descriptors.get(tool_name) or tool.descriptor()
            required_completion = (
                tool_name == "agent__complete"
                and policy.completion_policy == "explicit"
            )
            if not policy.tool_selection.allows(descriptor) and not required_completion:
                return False

            return True
        except Exception:
            return False

    def build_tool_context(
        self,
        *,
        conversation: Conversation,
        provider: Provider,
        approval_callback: Optional[Callable[[ToolApprovalRequest], Any]],
        questions_callback: Optional[Callable[[dict[str, Any]], Any]],
        llm_client: Any,
        policy: RunPolicy,
        tool_call_id: str | None = None,
        tool_name: str = "",
        debug_trace=None,
        compression_tasks: Any = None,
    ) -> ToolContext:
        """Build tool context with state.

        Args:
            conversation: Current conversation
            provider: LLM provider
            approval_callback: Callback for tool approval
            llm_client: LLM client instance

        Returns:
            Tool context for execution
        """
        work_dir = getattr(conversation, "work_dir", "") or "."
        source = str(getattr(policy, "source", "") or "desktop")
        mode = str(getattr(policy, "mode", "") or getattr(conversation, "mode", "") or "chat")
        agent_id = str(getattr(conversation, "id", "") or "")
        trace_id = str(tool_call_id or "")
        workspace_roots = (str(work_dir or "."),)
        try:
            tool = self._tool_manager.registry.get_tool(str(tool_name or ""))
        except Exception:
            tool = None
        category = normalize_tool_category(getattr(tool, "category", "capability") if tool is not None else "capability")
        risk = normalize_risk_level(getattr(tool, "risk", "high") if tool is not None else "high")
        permission = PermissionContext(
            source=source,
            mode=mode,
            tool_name=str(tool_name or ""),
            category=category,
            risk=risk,
            agent_id=agent_id,
            trace_id=trace_id,
            workspace_roots=workspace_roots,
        )
        runtime = ToolRuntimeContext(
            source=source,
            mode=mode,
            agent_id=agent_id,
            trace_id=trace_id,
            tool_call_id=str(tool_call_id or ""),
            workspace_roots=workspace_roots,
            permission=permission,
            capability_executor=self._capability_executor,
            compression_factory=self._compression_factory,
            compression_tasks=compression_tasks,
            shell_config=self._shell_config,
            process_manager=getattr(self._tool_manager, "processes", None),
            run_policy=policy,
            debug_trace=debug_trace,
        )

        # Extract state dict
        state_dict: dict[str, Any]
        try:
            state_dict = dict(conversation.get_state().to_dict() or {})
        except Exception:
            try:
                state_dict = dict(getattr(conversation, "_state_dict", {}) or {})
            except Exception:
                state_dict = {}

        # Add current seq_id
        try:
            state_dict["_current_seq"] = int(conversation.current_seq_id() or 0)
        except Exception:
            state_dict["_current_seq"] = 0

        return ToolContext(
            work_dir=work_dir,
            approval_callback=approval_callback,
            questions_callback=questions_callback,
            state=state_dict,
            llm_client=llm_client,
            conversation=conversation,
            provider=provider,
            content_service=self._content_service,
            runtime=runtime,
            permission=permission,
        )

    async def execute_tool(
        self,
        *,
        tool_name: str,
        tool_args: dict,
        allowed: bool,
        policy: RunPolicy,
        context: ToolContext,
    ) -> ToolResult:
        """Execute a tool with given arguments.

        Args:
            tool_name: Name of tool to execute
            tool_args: Tool arguments
            allowed: Whether tool is allowed by policy
            policy: Execution policy
            context: Tool context

        Returns:
            Tool execution result
        """
        if not allowed:
            tool = self._tool_manager.registry.get_tool(tool_name)
            category = str(getattr(tool, "category", "capability") or "capability") if tool is not None else "capability"
            tool_policy = policy.tool_permissions.resolve(tool_name, category)
            return ToolResult(
                f"Tool '{tool_name}' is disabled by current mode/settings. "
                f"(tool_policy={tool_policy.to_dict() if tool_policy else 'default'})",
                is_error=True,
            )

        try:
            if tool_name == "agent__complete" and policy.completion_policy != "explicit":
                return ToolResult("agent__complete is unavailable in text-completion modes.", is_error=True)
            tool = self._tool_manager.registry.get_tool(tool_name)
            if tool is None:
                return ToolResult(f"Tool '{tool_name}' not found.", is_error=True)

            risk = normalize_risk_level(tool.assess_risk(tool_args, context))
            permission = replace(
                context.permission,
                tool_name=tool.name,
                category=normalize_tool_category(tool.category),
                risk=risk,
            )
            context.permission = permission
            context.runtime = replace(context.runtime, permission=permission)

            if policy.tool_permissions.resolve(tool.name, tool.category).action == "ask":
                request = ToolApprovalRequest(
                    tool_name=tool.name,
                    tool_call_id=str(context.runtime.tool_call_id or ""),
                    arguments=dict(tool_args or {}),
                    category=permission.category,
                    risk=risk,
                    message=tool.approval_message(tool_args, context),
                )
                approved = await self._request_approval(
                    context.approval_callback,
                    request,
                )
                if not approved:
                    return ToolResult(
                        f"Permission denied for {tool.name} ({risk} risk).",
                        is_error=True,
                    )
            return await self._tool_manager.execute_tool_with_context(
                tool_name, tool_args, context
            )
        except Exception as e:
            logger.error("Tool execution failed: %s(%s) - %s", tool_name, tool_args, e)
            return ToolResult(f"Error executing tool {tool_name}: {e}", is_error=True)

    @staticmethod
    async def _request_approval(callback: Any, request: ToolApprovalRequest) -> bool:
        if callback is None:
            return False
        try:
            result = callback(request)
            if inspect.isawaitable(result):
                result = await result
            return bool(result)
        except Exception as exc:
            logger.warning("Tool approval callback failed: %s", exc)
            return False

    def sync_state(self, conversation: Conversation, context: ToolContext) -> None:
        """Sync state from tool context back to conversation.

        Args:
            conversation: Conversation to update
            context: Tool context with updated state
        """
        try:
            # Filter out internal state keys (starting with _)
            synced = {
                k: v for k, v in (context.state or {}).items()
                if not str(k).startswith("_")
            }

            # Update conversation state
            try:
                from models.contracts.session_state import SessionState
                conversation.set_state(SessionState.from_dict(dict(synced)))
            except Exception:
                try:
                    conversation._state_dict = dict(synced)
                except Exception as exc:
                    logger.debug("Failed to write fallback conversation state dict: %s", exc)
        except Exception as e:
            logger.warning("Failed to sync state: %s", e)

    def refresh_context_state(self, conversation: Conversation, context: ToolContext) -> None:
        """Refresh a tool context from the current conversation state.

        Some control actions, such as nested agents, update the conversation
        directly after the tool context was created. Refresh before the normal
        post-tool sync so an older context snapshot cannot erase those changes.
        """
        try:
            internal = {
                k: v for k, v in (context.state or {}).items()
                if str(k).startswith("_")
            }
            state_dict = dict(conversation.get_state().to_dict() or {})
            state_dict.update(internal)
            context.state.clear()
            context.state.update(state_dict)
        except Exception as e:
            logger.warning("Failed to refresh tool context state: %s", e)

    def attach_state_snapshot(self, conversation: Conversation, msg: Message) -> None:
        """Attach state snapshot to message.

        Args:
            conversation: Current conversation
            msg: Message to attach snapshot to
        """
        try:
            try:
                msg.state_snapshot = state_checkpoint(conversation.get_state())
            except Exception:
                msg.state_snapshot = {
                    "_snapshot_kind": "checkpoint",
                    "state_version": 0,
                    "last_updated_seq": 0,
                }
        except Exception:
            msg.state_snapshot = None
