"""Tool permission policy and approval wrapping helpers."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from core.config.schema import ToolPermissionConfig, ToolPolicy
from core.tools.base import BaseTool, ToolContext


@dataclass
class ToolPermissionPolicy:
    """Runtime permission policy backed by ToolPermissionConfig.

    Supports per-tool overrides in addition to category defaults.
    """

    config: ToolPermissionConfig = field(default_factory=ToolPermissionConfig)

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> "ToolPermissionPolicy":
        d = dict(config) if config is not None else {}
        # Support both full app_settings dict and permissions sub-dict
        if "permissions" in d:
            d = d.get("permissions") or {}
        return cls(config=ToolPermissionConfig.from_dict(d))

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
    """Wrap tool approval callbacks with a repository-wide permission policy."""

    def __init__(self, policy: ToolPermissionPolicy | None = None) -> None:
        self._policy = policy or ToolPermissionPolicy()

    @property
    def policy(self) -> ToolPermissionPolicy:
        return self._policy

    def update(self, config: dict[str, Any] | None) -> None:
        self._policy = ToolPermissionPolicy.from_config(config)

    def wrap_context(self, context: ToolContext, tool: BaseTool) -> ToolContext:
        return self.wrap_context_with_policy(context, tool, self._policy)

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

        return ToolContext(
            work_dir=context.work_dir,
            approval_callback=permission_aware_callback,
            questions_callback=getattr(context, "questions_callback", None),
            state=context.state,
            llm_client=getattr(context, "llm_client", None),
            conversation=getattr(context, "conversation", None),
            provider=getattr(context, "provider", None),
        )