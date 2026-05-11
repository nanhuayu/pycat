from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

from core.config.schema import ToolPermissionConfig
from core.llm.llm_config import LLMConfig
from core.task.types import RetryPolicy, RunPolicy
from core.tools.catalog import ToolSelectionPolicy

if TYPE_CHECKING:
    from models.conversation import Conversation


@dataclass(frozen=True)
class TurnPolicy:
    """UI/runtime-facing execution policy.

    Wraps ``RunPolicy`` while moving model/generation options into
    ``LLMConfig``, matching the planned TurnEngine contract.
    Tool visibility and auto-approval are controlled by effective tool
    permissions: per-tool overrides first, then category defaults.
    """

    mode: str = "chat"
    max_turns: int = 200
    context_window_limit: int = 100_000
    llm: LLMConfig = field(default_factory=LLMConfig)
    enable_thinking: bool = True
    tool_selection: ToolSelectionPolicy = field(default_factory=ToolSelectionPolicy.all)
    tool_permissions: ToolPermissionConfig = field(default_factory=ToolPermissionConfig)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    auto_compress_enabled: Optional[bool] = None

    @classmethod
    def from_run_policy(
        cls,
        policy: RunPolicy,
        *,
        conversation: "Conversation | None" = None,
    ) -> "TurnPolicy":
        llm = conversation.get_llm_config() if conversation is not None else LLMConfig()
        updates: dict[str, object] = {}
        if policy.model is not None:
            updates["model"] = str(policy.model)
        if policy.temperature is not None:
            updates["temperature"] = float(policy.temperature)
        if policy.max_tokens is not None:
            updates["max_tokens"] = int(policy.max_tokens)
        if updates:
            llm = llm.with_updates(**updates)

        return cls(
            mode=str(policy.mode or "chat"),
            max_turns=int(policy.max_turns or 200),
            context_window_limit=int(policy.context_window_limit or 100_000),
            llm=llm,
            enable_thinking=bool(policy.enable_thinking),
            tool_selection=getattr(policy, "tool_selection", ToolSelectionPolicy.all()),
            tool_permissions=getattr(policy, "tool_permissions", ToolPermissionConfig()),
            retry=policy.retry,
            auto_compress_enabled=policy.auto_compress_enabled,
        )

    def to_run_policy(self) -> RunPolicy:
        return RunPolicy(
            mode=str(self.mode or "chat"),
            max_turns=int(self.max_turns or 200),
            context_window_limit=int(self.context_window_limit or 100_000),
            enable_thinking=bool(self.enable_thinking),
            tool_selection=self.tool_selection,
            model=self.llm.model or None,
            temperature=self.llm.temperature,
            max_tokens=self.llm.max_tokens,
            tool_permissions=self.tool_permissions,
            retry=self.retry,
            auto_compress_enabled=self.auto_compress_enabled,
        )
