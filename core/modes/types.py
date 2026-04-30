from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple, Union


ModeSource = Literal["global", "project", "builtin"]


@dataclass(frozen=True)
class PromptComponent:
    role_definition: Optional[str] = None
    when_to_use: Optional[str] = None
    description: Optional[str] = None
    custom_instructions: Optional[str] = None


@dataclass(frozen=True)
class GroupOptions:
    file_regex: Optional[str] = None
    description: Optional[str] = None


GroupName = str
GroupEntry = Union[GroupName, Tuple[GroupName, GroupOptions]]

# All known tool groups.
TOOL_GROUPS = {"read", "edit", "command", "mcp", "search", "browser", "modes"}


@dataclass(frozen=True)
class ModeConfig:
    slug: str
    name: str
    role_definition: str
    when_to_use: Optional[str] = None
    description: Optional[str] = None
    custom_instructions: Optional[str] = None
    groups: Sequence[GroupEntry] = field(default_factory=tuple)
    tool_allowlist: Sequence[str] = field(default_factory=tuple)
    tool_denylist: Sequence[str] = field(default_factory=tuple)
    max_turns: Optional[int] = None
    context_window_limit: Optional[int] = None
    auto_compress_enabled: Optional[bool] = None
    source: Optional[ModeSource] = None

    def group_names(self) -> set[str]:
        """Return the flat set of group name strings."""
        names: set[str] = set()
        for g in self.groups or []:
            if isinstance(g, tuple) and g:
                names.add(str(g[0]))
            else:
                names.add(str(g))
        return names

    def has_group(self, name: str) -> bool:
        return name in self.group_names()


def normalize_mode_slug(raw: str) -> str:
    s = (raw or "").strip().lower()
    if not s:
        return "chat"
    return s


def safe_mode_display_name(mode: ModeConfig) -> str:
    return (mode.name or mode.slug or "mode").strip() or (mode.slug or "mode")
