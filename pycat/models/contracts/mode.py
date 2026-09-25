"""Mode and delegated-agent profile contracts."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional, Sequence

from pycat.models.contracts.model_target import ModelTarget
from pycat.models.contracts.tooling import TOOL_CATEGORIES, canonical_tool_categories, normalize_tool_category

ModeSource = Literal["global", "project", "builtin"]
ProfileKind = Literal["primary", "subagent", "both"]
SharedContextPolicy = Literal["indexes_only", "selected_artifacts", "full_session_readonly"]
CompletionPolicy = Literal["text", "explicit"]
MODES_SCHEMA_VERSION = 3
MODE_TOOL_CATEGORIES = set(TOOL_CATEGORIES)


@dataclass(frozen=True)
class ModeConfig:
    slug: str
    name: str
    purpose: str = ""
    prompt: str = ""
    allowed_tool_categories: Sequence[str] = field(default_factory=tuple)
    profile_kind: ProfileKind = "primary"
    completion_policy: CompletionPolicy = "text"
    model_target: ModelTarget = field(default_factory=ModelTarget)
    max_turns: Optional[int] = None
    shared_context_policy: SharedContextPolicy = "indexes_only"
    source: Optional[ModeSource] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_tool_categories", canonical_tool_categories(self.allowed_tool_categories))

        profile_kind = str(self.profile_kind or "primary").strip().lower()
        if profile_kind not in {"primary", "subagent", "both"}:
            profile_kind = "primary"
        object.__setattr__(self, "profile_kind", profile_kind)

        completion_policy = str(self.completion_policy or "text").strip().lower()
        if completion_policy not in {"text", "explicit"}:
            completion_policy = "text"
        object.__setattr__(self, "completion_policy", completion_policy)

        context_policy = str(self.shared_context_policy or "indexes_only").strip().lower()
        if context_policy not in {"indexes_only", "selected_artifacts", "full_session_readonly"}:
            context_policy = "indexes_only"
        object.__setattr__(self, "shared_context_policy", context_policy)

    def tool_category_names(self) -> set[str]:
        return {
            normalize_tool_category(item)
            for item in self.allowed_tool_categories
            if normalize_tool_category(item) in MODE_TOOL_CATEGORIES
        }

    def is_primary_mode(self) -> bool:
        return self.profile_kind in {"primary", "both"}

    def is_subagent_profile(self) -> bool:
        return self.profile_kind in {"subagent", "both"}


def normalize_mode_slug(raw: str) -> str:
    value = (raw or "").strip().lower()
    return value or "chat"
