from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Set, TYPE_CHECKING

from core.llm.llm_config import LLMConfig
from core.task.types import RetryPolicy, RunPolicy

if TYPE_CHECKING:
    from models.conversation import Conversation


@dataclass(frozen=True)
class TurnPolicy:
    """UI/runtime-facing execution policy.

    This wraps the legacy `RunPolicy` while moving model/generation options into
    `LLMConfig`, matching the planned TurnEngine contract.
    """

    mode: str = "chat"
    max_turns: int = 20
    context_window_limit: int = 100_000
    llm: LLMConfig = field(default_factory=LLMConfig)
    enable_thinking: bool = True
    enable_search: bool = False
    enable_mcp: bool = False
    tool_allowlist: Optional[Set[str]] = None
    tool_denylist: Optional[Set[str]] = None
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
            max_turns=int(policy.max_turns or 20),
            context_window_limit=int(policy.context_window_limit or 100_000),
            llm=llm,
            enable_thinking=bool(policy.enable_thinking),
            enable_search=bool(policy.enable_search),
            enable_mcp=bool(policy.enable_mcp),
            tool_allowlist=set(policy.tool_allowlist) if policy.tool_allowlist else None,
            tool_denylist=set(policy.tool_denylist) if policy.tool_denylist else None,
            retry=policy.retry,
            auto_compress_enabled=policy.auto_compress_enabled,
        )

    def to_run_policy(self) -> RunPolicy:
        return RunPolicy(
            mode=str(self.mode or "chat"),
            max_turns=int(self.max_turns or 20),
            context_window_limit=int(self.context_window_limit or 100_000),
            enable_thinking=bool(self.enable_thinking),
            enable_search=bool(self.enable_search),
            enable_mcp=bool(self.enable_mcp),
            model=self.llm.model or None,
            temperature=self.llm.temperature,
            max_tokens=self.llm.max_tokens,
            tool_allowlist=set(self.tool_allowlist) if self.tool_allowlist else None,
            tool_denylist=set(self.tool_denylist) if self.tool_denylist else None,
            retry=self.retry,
            auto_compress_enabled=self.auto_compress_enabled,
        )
