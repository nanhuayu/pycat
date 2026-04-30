"""Tool permission policy and approval wrapping helpers."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Optional

from core.tools.base import BaseTool, ToolContext


@dataclass
class ToolPermissionPolicy:
    auto_approve_read: bool = True
    auto_approve_edit: bool = False
    auto_approve_command: bool = False

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> "ToolPermissionPolicy":
        data = config or {}
        return cls(
            auto_approve_read=bool(data.get("auto_approve_read", True)),
            auto_approve_edit=bool(data.get("auto_approve_edit", False)),
            auto_approve_command=bool(data.get("auto_approve_command", False)),
        )

    def to_dict(self) -> dict[str, bool]:
        return {
            "auto_approve_read": bool(self.auto_approve_read),
            "auto_approve_edit": bool(self.auto_approve_edit),
            "auto_approve_command": bool(self.auto_approve_command),
        }

    def is_auto_approved(self, category: str) -> bool:
        if category == "read":
            return bool(self.auto_approve_read)
        if category == "edit":
            return bool(self.auto_approve_edit)
        if category == "command":
            return bool(self.auto_approve_command)
        return False


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
        original_callback = context.approval_callback

        async def permission_aware_callback(message: str) -> bool:
            if self._policy.is_auto_approved(tool.category):
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