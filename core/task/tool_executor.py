"""Tool execution module - handles tool calls and state management.

Extracted from task.py to reduce complexity.
Responsibilities:
- Parse tool calls from LLM responses
- Execute tools via MCP manager
- Build tool context with state
- Handle tool permissions
- Sync state after tool execution
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

from models.conversation import Conversation, Message
from models.provider import Provider

from core.tools.base import ToolContext, ToolResult
from core.tools.manager import ToolManager
from core.task.types import RunPolicy

logger = logging.getLogger(__name__)


class ToolExecutor:
    """Handles tool execution and state management."""

    def __init__(self, tool_manager: ToolManager):
        self._tool_manager = tool_manager

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
        1. Explicit per-tool policy in ``policy.tool_policies``.
        2. Group-level filter via ``policy.tool_groups``.
        3. Default: allowed.
        """
        try:
            tool = self._tool_manager.registry.get_tool(tool_name)

            # 1. Per-tool policy
            per_tool = (policy.tool_policies or {}).get(tool_name)
            if per_tool is not None:
                return bool(per_tool.enabled)

            # 2. Group filter
            if policy.tool_groups is not None and tool is not None:
                group = str(getattr(tool, "group", "") or "")
                if group not in policy.tool_groups:
                    return False

            return True
        except Exception:
            return False

    def build_tool_context(
        self,
        *,
        conversation: Conversation,
        provider: Provider,
        approval_callback: Optional[Callable[[str], bool]],
        questions_callback: Optional[Callable[[dict[str, Any]], Any]],
        llm_client: Any,
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
            approval_callback=approval_callback or (lambda _msg: False),
            questions_callback=questions_callback,
            state=state_dict,
            llm_client=llm_client,
            conversation=conversation,
            provider=provider,
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
            tool_policy = (policy.tool_policies or {}).get(tool_name)
            return ToolResult(
                f"Tool '{tool_name}' is disabled by current mode/settings. "
                f"(tool_policy={tool_policy.to_dict() if tool_policy else 'default'})"
            )

        try:
            return await self._tool_manager.execute_tool_with_context(
                tool_name, tool_args, context
            )
        except Exception as e:
            logger.error("Tool execution failed: %s(%s) - %s", tool_name, tool_args, e)
            return ToolResult(f"Error executing tool {tool_name}: {e}", is_error=True)

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
                from models.state import SessionState
                conversation.set_state(SessionState.from_dict(dict(synced)))
            except Exception:
                try:
                    conversation._state_dict = dict(synced)
                except Exception as exc:
                    logger.debug("Failed to write fallback conversation state dict: %s", exc)
        except Exception as e:
            logger.warning("Failed to sync state: %s", e)

    def attach_state_snapshot(self, conversation: Conversation, msg: Message) -> None:
        """Attach state snapshot to message.

        Args:
            conversation: Current conversation
            msg: Message to attach snapshot to
        """
        try:
            msg.state_snapshot = dict(getattr(conversation, "_state_dict", {}) or {})
        except Exception:
            msg.state_snapshot = None
