"""Tool permission policy and approval wrapping helpers."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict

from core.config.schema import ToolPermissionConfig, ToolPolicy
from core.tools.base import BaseTool, PermissionContext, ToolContext, ToolRuntimeContext
from core.tools.catalog import normalize_tool_category


@dataclass
class ToolPermissionPolicy:
    """Runtime permission policy backed by ToolPermissionConfig.

    Supports per-tool overrides in addition to category defaults.
    """

    config: ToolPermissionConfig = field(default_factory=ToolPermissionConfig)

    def to_dict(self) -> dict[str, Any]:
        return self.config.to_dict()

    def resolve(self, tool_name: str, category: str = "misc") -> ToolPolicy:
        """Return the effective policy for a tool."""
        return self.config.resolve(tool_name, category)

    def is_auto_approved(self, tool_name: str, category: str = "misc") -> bool:
        return self.config.is_auto_approved(tool_name, category)

    def is_enabled(self, tool_name: str, category: str = "misc") -> bool:
        return self.config.is_enabled(tool_name, category)

    @classmethod
    def from_effective(
        cls,
        *,
        category_defaults: Dict[str, ToolPolicy] | None = None,
        tools: Dict[str, ToolPolicy] | None = None,
    ) -> "ToolPermissionPolicy":
        """Build a policy from already-normalized runtime permission maps."""
        return cls(
            config=ToolPermissionConfig(
                category_defaults=dict(category_defaults or {}),
                tools=dict(tools or {}),
            )
        )


class ToolPermissionResolver:
    """Wrap a ToolContext with the request-scoped permission policy."""

    @staticmethod
    def wrap_context_with_policy(
        context: ToolContext,
        tool: BaseTool,
        policy: ToolPermissionPolicy,
    ) -> ToolContext:
        original_callback = context.approval_callback

        async def permission_aware_callback(message: str) -> bool:
            if policy.is_auto_approved(tool.name, tool.category):
                return True
            if not original_callback:
                return False
            if asyncio.iscoroutinefunction(original_callback):
                return await original_callback(message)
            result = original_callback(message)
            if asyncio.iscoroutine(result):
                return await result
            return bool(result)

        runtime = getattr(context, "runtime", None) or ToolRuntimeContext()
        permission = PermissionContext(
            source=str(getattr(runtime, "source", "") or getattr(context, "permission", PermissionContext()).source or "desktop"),
            mode=str(getattr(runtime, "mode", "") or getattr(context, "permission", PermissionContext()).mode or "chat"),
            tool_name=tool.name,
            category=normalize_tool_category(tool.category),
            risk=str(getattr(getattr(context, "permission", None), "risk", "") or "normal"),
            agent_id=str(getattr(runtime, "agent_id", "") or ""),
            trace_id=str(getattr(runtime, "trace_id", "") or ""),
            workspace_roots=tuple(getattr(runtime, "workspace_roots", ()) or (context.work_dir,)),
        )
        runtime = ToolRuntimeContext(
            source=permission.source,
            mode=permission.mode,
            agent_id=permission.agent_id,
            trace_id=permission.trace_id,
            tool_call_id=str(getattr(runtime, "tool_call_id", "") or ""),
            workspace_roots=permission.workspace_roots,
            permission=permission,
        )

        return ToolContext(
            work_dir=context.work_dir,
            approval_callback=permission_aware_callback,
            questions_callback=getattr(context, "questions_callback", None),
            state=context.state,
            llm_client=getattr(context, "llm_client", None),
            conversation=getattr(context, "conversation", None),
            provider=getattr(context, "provider", None),
            runtime=runtime,
            permission=permission,
        )
