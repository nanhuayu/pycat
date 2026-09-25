"""Budget-aware runtime context item model."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from pycat.core.llm.token_budget import estimate_tokens
from pycat.models.conversation import Message

DegradeFn = Callable[["ContextItem", int], "ContextItem | None"]


@dataclass(frozen=True)
class ContextItem:
    """A single candidate piece of prompt context."""

    id: str
    kind: str
    content: str
    priority: int = 100
    required: bool = False
    source_ref: str = ""
    relevance_score: float = 0.0
    freshness: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    token_estimate: int = 0
    degrade_fn: DegradeFn | None = None

    def __post_init__(self) -> None:
        if self.token_estimate <= 0:
            object.__setattr__(self, "token_estimate", estimate_tokens(self.content))

    def degrade(self, remaining_tokens: int) -> "ContextItem | None":
        if self.degrade_fn is not None:
            return self.degrade_fn(self, remaining_tokens)
        if self.required:
            return self
        if remaining_tokens <= 0:
            return None
        if self.token_estimate <= remaining_tokens:
            return self
        max_chars = max(120, remaining_tokens * 3)
        if len(self.content) <= max_chars:
            return self
        marker = "\n\n... [context item truncated by budget] ...\n\n"
        edge = max(40, (max_chars - len(marker)) // 2)
        truncated = self.content[:edge] + marker + self.content[-edge:]
        return ContextItem(
            id=self.id,
            kind=self.kind,
            content=truncated,
            priority=self.priority,
            required=self.required,
            source_ref=self.source_ref,
            relevance_score=self.relevance_score,
            freshness=self.freshness,
            metadata={**self.metadata, "degraded": True},
            token_estimate=min(remaining_tokens, estimate_tokens(truncated)),
        )

    def to_message(self) -> Message:
        return Message(
            role="user",
            content=self.content,
            metadata={
                "synthetic": True,
                "context_kind": self.kind,
                "context_item_id": self.id,
                "context_source_ref": self.source_ref,
                **dict(self.metadata or {}),
            },
        )


@dataclass
class ContextPack:
    """Result of packing context items into a prompt budget."""

    items: list[ContextItem] = field(default_factory=list)
    dropped: list[ContextItem] = field(default_factory=list)
    degraded: list[ContextItem] = field(default_factory=list)
    token_estimate: int = 0
    diagnostics: dict[str, Any] = field(default_factory=dict)

class ContextPacker:
    """Small deterministic priority packer for provider context."""

    def __init__(self, token_limit: int):
        self.token_limit = max(1, int(token_limit))

    def pack(self, items: list[ContextItem]) -> ContextPack:
        limit = self.token_limit
        pack = ContextPack(diagnostics={"limit": limit, "input_items": len(items)})
        ordered = sorted(
            items,
            key=lambda item: (
                not item.required,
                item.priority,
                -float(item.relevance_score or 0),
                -float(item.freshness or 0),
                item.id,
            ),
        )
        used = 0
        for item in ordered:
            remaining = limit - used
            candidate = item if item.token_estimate <= remaining else item.degrade(remaining)
            if candidate is None:
                pack.dropped.append(item)
                continue
            if candidate.token_estimate > remaining and not candidate.required:
                pack.dropped.append(item)
                continue
            pack.items.append(candidate)
            used += candidate.token_estimate
            if candidate.content != item.content or candidate.metadata.get("degraded"):
                pack.degraded.append(candidate)
        pack.token_estimate = used
        pack.diagnostics.update(
            {
                "packed_items": len(pack.items),
                "dropped_items": len(pack.dropped),
                "degraded_items": len(pack.degraded),
                "token_estimate": used,
            }
        )
        return pack
