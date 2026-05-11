from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional, Sequence, Tuple, Union

from core.tools.catalog import TOOL_CATEGORIES, normalize_tool_category


ModeSource = Literal["global", "project", "builtin"]


@dataclass(frozen=True)
class PromptComponent:
    role_definition: Optional[str] = None
    when_to_use: Optional[str] = None
    description: Optional[str] = None
    custom_instructions: Optional[str] = None


@dataclass(frozen=True)
class ToolCategoryOptions:
    file_regex: Optional[str] = None
    description: Optional[str] = None


ToolCategoryName = str
ToolCategoryEntry = Union[ToolCategoryName, Tuple[ToolCategoryName, ToolCategoryOptions]]

# All known mode-facing tool categories.
MODE_TOOL_CATEGORIES = set(TOOL_CATEGORIES)


@dataclass(frozen=True)
class ModeConfig:
    slug: str
    name: str
    role_definition: str
    when_to_use: Optional[str] = None
    description: Optional[str] = None
    custom_instructions: Optional[str] = None
    allowed_tool_categories: Sequence[ToolCategoryEntry] = field(default_factory=tuple)
    tool_allowlist: Sequence[str] = field(default_factory=tuple)
    tool_denylist: Sequence[str] = field(default_factory=tuple)
    max_turns: Optional[int] = None
    context_window_limit: Optional[int] = None
    auto_compress_enabled: Optional[bool] = None
    source: Optional[ModeSource] = None

    def tool_category_names(self) -> set[str]:
        """Return the flat canonical set of tool category names."""
        names: set[str] = set()
        for item in self.allowed_tool_categories or []:
            if isinstance(item, tuple) and item:
                raw = str(item[0])
            else:
                raw = str(item)
            category = normalize_tool_category(raw)
            if category in MODE_TOOL_CATEGORIES:
                names.add(category)
        return names

    def allows_tool_category(self, name: str) -> bool:
        return normalize_tool_category(name) in self.tool_category_names()


def normalize_mode_slug(raw: str) -> str:
    s = (raw or "").strip().lower()
    if not s:
        return "chat"
    return s


def safe_mode_display_name(mode: ModeConfig) -> str:
    return (mode.name or mode.slug or "mode").strip() or (mode.slug or "mode")
