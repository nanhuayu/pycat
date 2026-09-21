"""Tooling contracts shared by config, runtime, UI, and tools."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Literal, Mapping, Optional, Set


TOOL_CATEGORIES: tuple[str, ...] = (
    "read",
    "web",
    "edit",
    "execute",
    "state",
    "delegate",
    "capability",
    "mcp",
)

TOOL_CATEGORY_LABELS: dict[str, str] = {
    "read": "读取",
    "web": "联网",
    "edit": "编辑",
    "execute": "执行",
    "state": "状态与交互",
    "delegate": "委托",
    "capability": "能力",
    "mcp": "MCP",
}

TOOL_CATEGORY_SORT_ORDER: dict[str, int] = {
    name: index for index, name in enumerate(TOOL_CATEGORIES)
}

RiskLevel = Literal["low", "medium", "high"]
RISK_LEVELS: tuple[RiskLevel, ...] = ("low", "medium", "high")
RISK_LEVEL_LABELS: dict[str, str] = {
    "low": "低风险",
    "medium": "中风险",
    "high": "高风险",
}

PermissionAction = Literal["deny", "ask", "allow"]
PERMISSION_ACTIONS: tuple[PermissionAction, ...] = ("deny", "ask", "allow")
ToolApprovalMode = Literal["default", "ask", "allow", "deny", "custom"]
TOOL_APPROVAL_MODES: tuple[ToolApprovalMode, ...] = (
    "default",
    "ask",
    "allow",
    "deny",
    "custom",
)
FilesystemScopeMode = Literal["confined", "full_access"]
FILESYSTEM_SCOPE_MODES: tuple[FilesystemScopeMode, ...] = ("confined", "full_access")

# Product defaults for new sessions. Invalid input and legacy deserialization
# retain their conservative fallbacks; neither silently expands saved access.
DEFAULT_TOOL_APPROVAL: ToolApprovalMode = "allow"
DEFAULT_FILESYSTEM_MODE: FilesystemScopeMode = "full_access"


def normalize_tool_category(category: str | None) -> str:
    raw = str(category or "").strip().lower()
    return raw if raw in TOOL_CATEGORIES else "capability"


def normalize_risk_level(value: str | None) -> RiskLevel:
    raw = str(value or "").strip().lower()
    if raw in RISK_LEVELS:
        return raw  # type: ignore[return-value]
    return "low"


def normalize_permission_action(value: str | None) -> PermissionAction:
    raw = str(value or "").strip().lower()
    if raw in PERMISSION_ACTIONS:
        return raw  # type: ignore[return-value]
    return "ask"


def normalize_tool_approval(value: str | None) -> ToolApprovalMode:
    raw = str(value or "").strip().lower()
    if raw in TOOL_APPROVAL_MODES:
        return raw  # type: ignore[return-value]
    return "default"


def normalize_filesystem_scope_mode(value: str | None) -> FilesystemScopeMode:
    raw = str(value or "").strip().lower()
    if raw in FILESYSTEM_SCOPE_MODES:
        return raw  # type: ignore[return-value]
    return "confined"


@dataclass(frozen=True)
class FilesystemScope:
    """Filesystem ceiling carried by one run policy.

    ``allow_home_read`` is granted only by interactive local entry points.
    ``granted_read_roots`` contains canonical, run-local roots. Workspace roots
    are derived by the tool boundary and are not persisted here.
    """

    mode: FilesystemScopeMode = "confined"
    allow_home_read: bool = False
    granted_read_roots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        roots: list[str] = []
        seen: set[str] = set()
        for item in self.granted_read_roots or ():
            value = str(item or "").strip()
            key = value.casefold()
            if not value or key in seen:
                continue
            seen.add(key)
            roots.append(value)
        object.__setattr__(self, "mode", normalize_filesystem_scope_mode(self.mode))
        object.__setattr__(self, "allow_home_read", bool(self.allow_home_read))
        object.__setattr__(self, "granted_read_roots", tuple(roots))

    @property
    def is_full_access(self) -> bool:
        return self.mode == "full_access"


def _clean_set(values: Iterable[str] | None) -> Optional[Set[str]]:
    if values is None:
        return None
    return {str(item).strip() for item in values if str(item or "").strip()}


@dataclass(frozen=True)
class ToolSelectionPolicy:
    """Request-time restriction. Each populated field narrows visibility."""

    allowed_categories: Optional[Set[str]] = None
    allowed_tools: Optional[Set[str]] = None
    allowed_sources: Optional[Set[str]] = None
    denied_tools: Set[str] = field(default_factory=set)
    require_available: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "allowed_categories",
            {normalize_tool_category(value) for value in self.allowed_categories}
            if self.allowed_categories is not None
            else None,
        )
        object.__setattr__(self, "allowed_tools", _clean_set(self.allowed_tools))
        object.__setattr__(self, "allowed_sources", _clean_set(self.allowed_sources))
        object.__setattr__(self, "denied_tools", _clean_set(self.denied_tools) or set())

    @classmethod
    def all(cls) -> "ToolSelectionPolicy":
        return cls()

    @classmethod
    def from_categories(cls, categories: Iterable[str] | None) -> "ToolSelectionPolicy":
        if categories is None:
            return cls()
        return cls(allowed_categories={normalize_tool_category(item) for item in categories})

    def with_categories(self, categories: Iterable[str] | None) -> "ToolSelectionPolicy":
        return ToolSelectionPolicy(
            allowed_categories={normalize_tool_category(item) for item in categories or ()},
            allowed_tools=self.allowed_tools,
            allowed_sources=self.allowed_sources,
            denied_tools=self.denied_tools,
            require_available=self.require_available,
        )

    def intersect(self, other: "ToolSelectionPolicy | None") -> "ToolSelectionPolicy":
        if other is None:
            return self

        def intersection(left: Optional[Set[str]], right: Optional[Set[str]]) -> Optional[Set[str]]:
            if left is None:
                return None if right is None else set(right)
            if right is None:
                return set(left)
            return set(left) & set(right)

        return ToolSelectionPolicy(
            allowed_categories=intersection(self.allowed_categories, other.allowed_categories),
            allowed_tools=intersection(self.allowed_tools, other.allowed_tools),
            allowed_sources=intersection(self.allowed_sources, other.allowed_sources),
            denied_tools=set(self.denied_tools) | set(other.denied_tools),
            require_available=self.require_available or other.require_available,
        )

    def allows(self, descriptor: "ToolDescriptor") -> bool:
        if descriptor.name in self.denied_tools:
            return False
        if self.allowed_categories is not None and descriptor.category not in self.allowed_categories:
            return False
        if self.allowed_tools is not None and descriptor.name not in self.allowed_tools:
            return False
        if self.allowed_sources is not None and descriptor.source not in self.allowed_sources:
            return False
        return not self.require_available or descriptor.available

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed_categories": sorted(self.allowed_categories) if self.allowed_categories is not None else None,
            "allowed_tools": sorted(self.allowed_tools) if self.allowed_tools is not None else None,
            "allowed_sources": sorted(self.allowed_sources) if self.allowed_sources is not None else None,
            "denied_tools": sorted(self.denied_tools),
            "require_available": bool(self.require_available),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "ToolSelectionPolicy":
        payload = data if isinstance(data, Mapping) else {}
        raw_categories = payload.get("allowed_categories")
        raw_tools = payload.get("allowed_tools")
        raw_sources = payload.get("allowed_sources")
        raw_denied_tools = payload.get("denied_tools")
        return cls(
            allowed_categories=set(raw_categories or ()) if raw_categories is not None else None,
            allowed_tools=set(raw_tools or ()) if raw_tools is not None else None,
            allowed_sources=set(raw_sources or ()) if raw_sources is not None else None,
            denied_tools=set(raw_denied_tools or ()),
            require_available=bool(payload.get("require_available", True)),
        )


@dataclass(frozen=True)
class ToolPolicy:
    """Tri-state permission for one category or tool.

    ``deny`` means invisible and non-executable, ``ask`` requires explicit
    approval per call, ``allow`` executes directly.
    """

    action: PermissionAction = "ask"

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", normalize_permission_action(self.action))

    @staticmethod
    def from_dict(data: Mapping[str, Any] | None) -> "ToolPolicy":
        payload = dict(data) if isinstance(data, Mapping) else {}
        return ToolPolicy(action=normalize_permission_action(str(payload.get("action") or "ask")))

    def to_dict(self) -> Dict[str, Any]:
        return {"action": self.action}


def default_tool_category_policies() -> Dict[str, ToolPolicy]:
    return {
        "read": ToolPolicy(action="allow"),
        "web": ToolPolicy(action="allow"),
        "edit": ToolPolicy(action="ask"),
        "execute": ToolPolicy(action="ask"),
        "state": ToolPolicy(action="allow"),
        "delegate": ToolPolicy(action="ask"),
        "capability": ToolPolicy(action="allow"),
        "mcp": ToolPolicy(action="ask"),
    }


@dataclass(frozen=True)
class ToolPermissionConfig:
    """Per-category/per-tool tri-state permission rules.

    A tool rule replaces its category rule outright (explicit user intent
    wins); there is no runtime preset field — session presets are expanded
    into a concrete config by the policy builder before a run starts.
    """

    category_defaults: Dict[str, ToolPolicy] = field(default_factory=default_tool_category_policies)
    tools: Dict[str, ToolPolicy] = field(default_factory=dict)

    def __post_init__(self) -> None:
        defaults = default_tool_category_policies()
        for category, policy in (self.category_defaults or {}).items():
            normalized = normalize_tool_category(category)
            defaults[normalized] = policy if isinstance(policy, ToolPolicy) else ToolPolicy.from_dict(policy)
        object.__setattr__(self, "category_defaults", defaults)
        object.__setattr__(
            self,
            "tools",
            {
                str(name): policy if isinstance(policy, ToolPolicy) else ToolPolicy.from_dict(policy)
                for name, policy in (self.tools or {}).items()
            },
        )

    @staticmethod
    def from_dict(data: Mapping[str, Any] | None) -> "ToolPermissionConfig":
        payload = dict(data) if isinstance(data, Mapping) else {}
        tools: Dict[str, ToolPolicy] = {}
        raw_tools = payload.get("tools") if isinstance(payload.get("tools"), Mapping) else {}
        for name, policy_data in raw_tools.items():
            if name and not str(name).startswith("$"):
                tools[str(name)] = ToolPolicy.from_dict(policy_data)

        defaults = default_tool_category_policies()
        raw_defaults = payload.get("category_defaults")
        if isinstance(raw_defaults, Mapping):
            for name, policy_data in raw_defaults.items():
                if str(name) in TOOL_CATEGORIES and isinstance(policy_data, Mapping):
                    defaults[str(name)] = ToolPolicy.from_dict(policy_data)
        return ToolPermissionConfig(category_defaults=defaults, tools=tools)

    @staticmethod
    def from_settings_dict(settings: Mapping[str, Any] | None) -> "ToolPermissionConfig":
        payload = dict(settings) if isinstance(settings, Mapping) else {}
        permissions = payload.get("permissions")
        if isinstance(permissions, Mapping):
            return ToolPermissionConfig.from_dict(permissions)
        return ToolPermissionConfig()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category_defaults": {name: policy.to_dict() for name, policy in self.category_defaults.items()},
            "tools": {name: policy.to_dict() for name, policy in self.tools.items()},
        }

    def resolve(self, tool_name: str, category: str = "capability") -> ToolPolicy:
        category_policy = self.category_defaults.get(normalize_tool_category(category), ToolPolicy())
        return self.tools.get(tool_name, category_policy)

def permission_config_for_approval(
    approval: str | None,
    custom: "ToolPermissionConfig | None" = None,
) -> "ToolPermissionConfig":
    """Expand one session tool-approval mode into a concrete config.

    ``default`` uses the built-in category table, ``ask``/``deny`` apply one
    action to every category, ``allow`` allows every visible tool, and
    ``custom`` uses the application-level rules maintained in settings.
    """
    normalized = normalize_tool_approval(approval)
    if normalized == "custom":
        return custom or ToolPermissionConfig()
    if normalized == "default":
        return ToolPermissionConfig()
    return ToolPermissionConfig(
        category_defaults={
            name: ToolPolicy(action=normalized) for name in TOOL_CATEGORIES
        }
    )


def filesystem_scope_for_mode(
    mode: str | None,
    *,
    allow_home_read: bool = False,
) -> FilesystemScope:
    return FilesystemScope(
        mode=normalize_filesystem_scope_mode(mode),
        allow_home_read=allow_home_read,
    )


@dataclass(frozen=True)
class ToolAvailabilityContext:
    work_dir: str = ""
    data_dir: str = ""
    conversation_id: str = ""
    source: str = "desktop"
    filesystem_mode: FilesystemScopeMode = "confined"
    search_available: bool = False
    mcp_available: bool = False
    completion_policy: str = ""
    memory_enabled: bool = True
    channel_file_delivery: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "filesystem_mode",
            normalize_filesystem_scope_mode(self.filesystem_mode),
        )


@dataclass(frozen=True)
class ToolDescriptor:
    name: str
    display_name: str
    description: str
    category: str
    source: str = "builtin"
    available: bool = True
    risk: RiskLevel = "low"
    virtual: bool = False
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
        return cls(
            name=str(getattr(tool, "name", "") or ""),
            display_name=display_name or str(getattr(tool, "display_name", "") or getattr(tool, "name", "")),
            description=str(getattr(tool, "description", "") or ""),
            category=normalize_tool_category(getattr(tool, "category", "capability")),
            source=source,
            available=bool(available),
            risk=normalize_risk_level(getattr(tool, "risk", "low")),
            virtual=bool(virtual),
            sort_order=int(sort_order),
            metadata=dict(metadata or {}),
        )
