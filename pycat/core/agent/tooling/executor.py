"""Tool execution module - handles tool calls and state management.

Owns the unified selection and permission boundary around ToolManager calls.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
from dataclasses import replace
from typing import Any, Callable, Optional

from pycat.core.tools.base import (
    ApprovalDecision,
    PermissionContext,
    ToolApprovalRequest,
    ToolContext,
    ToolResult,
    ToolRuntimeContext,
)
from pycat.core.tools.manager import ToolManager
from pycat.models.contracts.agent import RunPolicy
from pycat.models.contracts.session_state import SessionState
from pycat.models.contracts.tooling import normalize_risk_level, normalize_tool_category
from pycat.models.conversation import Conversation, Message
from pycat.models.provider import Provider
from pycat.models.session_paths import normalize_work_dir

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
            # Availability is a request-schema concern.  Execution still needs
            # to produce the tool's normal permission result for a direct call;
            # the tool itself enforces its workspace boundary.
            descriptor = tool.descriptor()
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
        cancel_event: Any = None,
        run_control: Any = None,
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
        work_dir = normalize_work_dir(getattr(conversation, "work_dir", ""))
        source = str(getattr(policy, "source", "") or "desktop")
        mode = str(getattr(policy, "mode", "") or getattr(conversation, "mode", "") or "chat")
        agent_id = str(getattr(conversation, "id", "") or "")
        trace_id = str(tool_call_id or "")
        workspace_roots = (work_dir,) if work_dir else ()
        filesystem_scope = policy.filesystem_scope
        read_roots = (
            *workspace_roots,
            *filesystem_scope.granted_read_roots,
        )
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
            read_roots=read_roots,
            filesystem_mode=filesystem_scope.mode,
        )
        runtime = ToolRuntimeContext(
            source=source,
            mode=mode,
            agent_id=agent_id,
            trace_id=trace_id,
            tool_call_id=str(tool_call_id or ""),
            workspace_roots=workspace_roots,
            read_roots=read_roots,
            filesystem_scope=filesystem_scope,
            permission=permission,
            capability_executor=self._capability_executor,
            compression_factory=self._compression_factory,
            compression_tasks=compression_tasks,
            shell_config=self._shell_config,
            process_manager=getattr(self._tool_manager, "processes", None),
            run_policy=policy,
            debug_trace=debug_trace,
            cancel_event=cancel_event,
            run_control=run_control,
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
            workspace_service=getattr(self._tool_manager, "workspace_service", None),
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
            requested_read_path = ""
            read_path_resolver = getattr(tool, "requested_read_path", None)
            if callable(read_path_resolver):
                requested_read_path = str(read_path_resolver(tool_args) or "").strip()
            external_read = None
            if requested_read_path and not requested_read_path.startswith("input:"):
                external_read = await asyncio.to_thread(context.external_read_request, requested_read_path)
            if external_read is not None and context.runtime.source not in {"desktop", "cli", "sdk"}:
                return ToolResult(
                    "Reading outside the inherited filesystem scope is unavailable "
                    f"for {context.runtime.source} runs.",
                    is_error=True,
                )
            requires_tool_approval = (
                policy.tool_permissions.resolve(tool.name, tool.category).action == "ask"
            )
            if external_read is not None and risk == "low":
                risk = "medium"
            permission = replace(
                context.permission,
                tool_name=tool.name,
                category=normalize_tool_category(tool.category),
                risk=risk,
            )
            context.permission = permission
            context.runtime = replace(context.runtime, permission=permission)

            if requires_tool_approval or external_read is not None:
                external_path = str(external_read[0]) if external_read is not None else ""
                read_grant_root = str(external_read[1]) if external_read is not None else ""
                message_parts: list[str] = []
                if requires_tool_approval:
                    message_parts.append(tool.approval_message(tool_args, context))
                if external_path:
                    message_parts.append(
                        "该调用需要读取当前 workspace 和用户目录之外的路径：\n"
                        f"{external_path}"
                    )
                request = ToolApprovalRequest(
                    tool_name=tool.name,
                    tool_call_id=str(context.runtime.tool_call_id or ""),
                    arguments=dict(tool_args or {}),
                    category=permission.category,
                    risk=risk,
                    message="\n\n".join(message_parts),
                    requires_tool_approval=requires_tool_approval,
                    external_path=external_path,
                    read_grant_root=read_grant_root,
                )
                decision = await self._request_approval(
                    context.approval_callback,
                    request,
                )
                if not decision.approved:
                    return ToolResult(
                        f"Permission denied for {tool.name} ({risk} risk).",
                        is_error=True,
                    )
                run_control = context.runtime.run_control
                if run_control is not None:
                    _permissions, live_scope, _revision = run_control.access_snapshot()
                    context.apply_filesystem_scope(live_scope)
                if external_read is not None:
                    read_scope = decision.read_scope or "call"
                    granted_root = external_read[0]
                    if read_scope == "run":
                        if run_control is None or run_control.add_read_grant(str(external_read[1])) is None:
                            return ToolResult(
                                "The run ended before the external read grant could be applied.",
                                is_error=True,
                            )
                        granted_root = external_read[1]
                    context.add_call_read_root(granted_root)
            return await self._tool_manager.execute_tool_with_context(
                tool_name, tool_args, context
            )
        except Exception as e:
            logger.error("Tool execution failed: %s(%s) - %s", tool_name, tool_args, e)
            return ToolResult(f"Error executing tool {tool_name}: {e}", is_error=True)

    @staticmethod
    async def _request_approval(
        callback: Any,
        request: ToolApprovalRequest,
    ) -> ApprovalDecision:
        if callback is None:
            return ApprovalDecision()
        try:
            result = callback(request)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, ApprovalDecision):
                return result
            if isinstance(result, dict):
                return ApprovalDecision(
                    approved=bool(result.get("approved")),
                    read_scope=str(result.get("read_scope") or ""),
                )
            approved = bool(result)
            return ApprovalDecision(
                approved=approved,
                read_scope="call" if approved and request.requires_path_approval else "",
            )
        except Exception as exc:
            logger.warning("Tool approval callback failed: %s", exc)
            return ApprovalDecision()

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
            msg.state_snapshot = conversation.get_state().checkpoint()
        except Exception:
            msg.state_snapshot = {
                "_snapshot_kind": "checkpoint",
                "state_version": 0,
                "last_updated_seq": 0,
            }
