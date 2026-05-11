"""Canonical tool catalog, categories, and request-time selection policy."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, Set


TOOL_CATEGORIES: tuple[str, ...] = (
    "read",
    "search",
    "edit",
    "execute",
    "manage",
    "delegate",
    "extension",
    "mcp",
)

TOOL_CATEGORY_LABELS: dict[str, str] = {
    "read": "读取类",
    "search": "搜索类",
    "edit": "编辑类",
    "execute": "执行类",
    "manage": "管理类",
    "delegate": "委托类",
    "extension": "扩展类",
    "mcp": "MCP 类",
}

TOOL_CATEGORY_SORT_ORDER: dict[str, int] = {
    name: index for index, name in enumerate(TOOL_CATEGORIES)
}


def normalize_tool_category(category: str | None) -> str:
    """Return the canonical permission category for a legacy or new category."""
    raw = str(category or "").strip().lower()
    if raw == "command":
        return "execute"
    if raw == "misc":
        return "extension"
    if raw in {"mode", "modes", "control", "workflow", "state"}:
        return "manage"
    if raw in TOOL_CATEGORIES:
        return raw
    return "extension"


def _clean_set(values: Iterable[str] | None) -> Optional[Set[str]]:
    if values is None:
        return None
    cleaned = {str(item or "").strip() for item in values if str(item or "").strip()}
    return cleaned or set()


@dataclass(frozen=True)
class ToolSelectionPolicy:
    """Request-time tool visibility policy.

    The policy is the single place that answers which categories, concrete
    tools, and sources may be exposed to the model for one run. It replaces
    scattered per-feature booleans and the older group/category split in
    runtime layers.
    """

    allowed_categories: Optional[Set[str]] = None
    allowed_tools: Optional[Set[str]] = None
    allowed_sources: Optional[Set[str]] = None
    prepared_queries: tuple[str, ...] = ()
    require_available: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "allowed_categories",
            {normalize_tool_category(v) for v in self.allowed_categories} if self.allowed_categories is not None else None,
        )
        object.__setattr__(self, "allowed_tools", _clean_set(self.allowed_tools))
        object.__setattr__(self, "allowed_sources", _clean_set(self.allowed_sources))
        object.__setattr__(
            self,
            "prepared_queries",
            tuple(str(item or "").strip() for item in self.prepared_queries if str(item or "").strip()),
        )

    @classmethod
    def all(cls) -> "ToolSelectionPolicy":
        return cls()

    @classmethod
    def from_categories(
        cls,
        categories: Iterable[str] | None,
        *,
        prepared_queries: Iterable[str] | None = None,
    ) -> "ToolSelectionPolicy":
        if categories is None:
            return cls(prepared_queries=tuple(prepared_queries or ()))
        return cls(
            allowed_categories={normalize_tool_category(category) for category in categories},
            prepared_queries=tuple(prepared_queries or ()),
        )

    def with_categories(self, categories: Iterable[str] | None) -> "ToolSelectionPolicy":
        return ToolSelectionPolicy(
            allowed_categories={normalize_tool_category(category) for category in categories or ()},
            allowed_tools=self.allowed_tools,
            allowed_sources=self.allowed_sources,
            prepared_queries=self.prepared_queries,
            require_available=self.require_available,
        )

    def with_prepared_queries(self, queries: Iterable[str] | None) -> "ToolSelectionPolicy":
        return ToolSelectionPolicy(
            allowed_categories=self.allowed_categories,
            allowed_tools=self.allowed_tools,
            allowed_sources=self.allowed_sources,
            prepared_queries=tuple(queries or ()),
            require_available=self.require_available,
        )

    def allows(self, descriptor: "ToolDescriptor") -> bool:
        if self.allowed_categories is not None and descriptor.category not in self.allowed_categories:
            return False
        if self.allowed_tools is not None and descriptor.name not in self.allowed_tools:
            return False
        if self.allowed_sources is not None and descriptor.source not in self.allowed_sources:
            return False
        if self.require_available and not descriptor.available:
            return False
        return True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed_categories": sorted(self.allowed_categories) if self.allowed_categories is not None else None,
            "allowed_tools": sorted(self.allowed_tools) if self.allowed_tools is not None else None,
            "allowed_sources": sorted(self.allowed_sources) if self.allowed_sources is not None else None,
            "prepared_queries": list(self.prepared_queries),
            "require_available": bool(self.require_available),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any] | None) -> "ToolSelectionPolicy":
        payload = data if isinstance(data, dict) else {}
        raw_categories = payload.get("allowed_categories")
        return cls(
            allowed_categories=set(raw_categories or ()) if raw_categories is not None else None,
            allowed_tools=set(payload.get("allowed_tools") or ()) if payload.get("allowed_tools") is not None else None,
            allowed_sources=set(payload.get("allowed_sources") or ()) if payload.get("allowed_sources") is not None else None,
            prepared_queries=tuple(payload.get("prepared_queries") or ()),
            require_available=bool(payload.get("require_available", True)),
        )


@dataclass(frozen=True)
class ToolAvailabilityContext:
    """Runtime availability facts independent from permissions."""

    work_dir: str = "."
    conversation_id: str = ""
    search_available: bool = False
    mcp_available: bool = False


@dataclass(frozen=True)
class ToolDescriptor:
    name: str
    display_name: str
    description: str
    category: str
    source: str = "builtin"
    available: bool = True
    virtual: bool = False
    aliases: tuple[str, ...] = ()
    sort_order: int = 1000
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_tool(
        cls,
        tool: Any,
        *,
        source: str = "builtin",
        available: bool = True,
        display_name: str | None = None,
        virtual: bool = False,
        sort_order: int = 1000,
        metadata: Dict[str, Any] | None = None,
    ) -> "ToolDescriptor":
        category = normalize_tool_category(getattr(tool, "category", "extension"))
        return cls(
            name=str(getattr(tool, "name", "") or ""),
            display_name=display_name or str(getattr(tool, "display_name", "") or getattr(tool, "name", "")),
            description=str(getattr(tool, "description", "") or ""),
            category=category,
            source=source,
            available=bool(available),
            virtual=bool(virtual),
            aliases=tuple(getattr(tool, "aliases", ()) or ()),
            sort_order=int(sort_order),
            metadata=dict(metadata or {}),
        )
