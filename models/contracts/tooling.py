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
ApprovalMode = Literal["standard", "developer_trust", "allow_all", "custom"]
RISK_LEVELS: tuple[RiskLevel, ...] = ("low", "medium", "high")
RISK_LEVEL_LABELS: dict[str, str] = {
    "low": "低风险",
    "medium": "中风险",
    "high": "高风险",
}


def normalize_tool_category(category: str | None) -> str:
    raw = str(category or "").strip().lower()
    return raw if raw in TOOL_CATEGORIES else "capability"


def normalize_risk_level(value: str | None) -> RiskLevel:
    raw = str(value or "").strip().lower()
    if raw in RISK_LEVELS:
        return raw  # type: ignore[return-value]
    return "low"


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
            require_available=self.require_available or other.require_available,
        )

    def allows(self, descriptor: "ToolDescriptor") -> bool:
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
            "require_available": bool(self.require_available),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "ToolSelectionPolicy":
        payload = data if isinstance(data, Mapping) else {}
        raw_categories = payload.get("allowed_categories")
        raw_tools = payload.get("allowed_tools")
        raw_sources = payload.get("allowed_sources")
        return cls(
            allowed_categories=set(raw_categories or ()) if raw_categories is not None else None,
            allowed_tools=set(raw_tools or ()) if raw_tools is not None else None,
            allowed_sources=set(raw_sources or ()) if raw_sources is not None else None,
            require_available=bool(payload.get("require_available", True)),
        )


@dataclass(frozen=True)
class ToolPolicy:
    enabled: bool = True
    auto_approve: bool = False

    @staticmethod
    def from_dict(data: Mapping[str, Any] | None) -> "ToolPolicy":
        payload = dict(data) if isinstance(data, Mapping) else {}
        return ToolPolicy(
            enabled=_as_bool(payload.get("enabled"), True),
            auto_approve=_as_bool(payload.get("auto_approve"), False),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"enabled": bool(self.enabled), "auto_approve": bool(self.auto_approve)}


def default_tool_category_policies() -> Dict[str, ToolPolicy]:
    return {
        "read": ToolPolicy(enabled=True, auto_approve=True),
        "web": ToolPolicy(enabled=True, auto_approve=True),
        "edit": ToolPolicy(enabled=True, auto_approve=False),
        "execute": ToolPolicy(enabled=True, auto_approve=False),
        "state": ToolPolicy(enabled=True, auto_approve=True),
        "delegate": ToolPolicy(enabled=True, auto_approve=False),
        "capability": ToolPolicy(enabled=True, auto_approve=True),
        "mcp": ToolPolicy(enabled=True, auto_approve=False),
    }


@dataclass(frozen=True)
class ToolPermissionConfig:
    approval_mode: ApprovalMode = "standard"
    category_defaults: Dict[str, ToolPolicy] = field(default_factory=default_tool_category_policies)
    tools: Dict[str, ToolPolicy] = field(default_factory=dict)

    def __post_init__(self) -> None:
        approval_mode = str(self.approval_mode or "standard").strip().lower()
        if approval_mode not in {"standard", "developer_trust", "allow_all", "custom"}:
            approval_mode = "standard"
        object.__setattr__(self, "approval_mode", approval_mode)
        defaults = default_tool_category_policies()
        for category, policy in (self.category_defaults or {}).items():
            normalized = normalize_tool_category(category)
            defaults[normalized] = policy
        object.__setattr__(self, "category_defaults", defaults)
        object.__setattr__(self, "tools", dict(self.tools or {}))

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
        return ToolPermissionConfig(
            approval_mode=str(payload.get("approval_mode") or "standard"),
            category_defaults=defaults,
            tools=tools,
        )

    @staticmethod
    def from_settings_dict(settings: Mapping[str, Any] | None) -> "ToolPermissionConfig":
        payload = dict(settings) if isinstance(settings, Mapping) else {}
        permissions = payload.get("permissions")
        if isinstance(permissions, Mapping):
            return ToolPermissionConfig.from_dict(permissions)
        return ToolPermissionConfig.from_dict(payload)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approval_mode": self.approval_mode,
            "category_defaults": {name: policy.to_dict() for name, policy in self.category_defaults.items()},
            "tools": {name: policy.to_dict() for name, policy in self.tools.items()},
        }

    def resolve(self, tool_name: str, category: str = "capability") -> ToolPolicy:
        category_policy = self.category_defaults.get(normalize_tool_category(category), ToolPolicy())
        override = self.tools.get(tool_name)
        if override is None:
            return category_policy
        return ToolPolicy(
            enabled=category_policy.enabled and override.enabled,
            auto_approve=category_policy.auto_approve and override.auto_approve,
        )

    def is_enabled(self, tool_name: str, category: str = "capability") -> bool:
        return self.resolve(tool_name, category).enabled

    def is_auto_approved(self, tool_name: str, category: str = "capability") -> bool:
        return self.resolve(tool_name, category).auto_approve

    def requires_approval(self, tool_name: str, category: str, risk: str) -> bool:
        if self.approval_mode == "allow_all":
            return False
        if normalize_risk_level(risk) == "high":
            return True
        if self.approval_mode == "developer_trust":
            return False
        return not self.resolve(tool_name, category).auto_approve


@dataclass(frozen=True)
class ToolAvailabilityContext:
    work_dir: str = "."
    conversation_id: str = ""
    search_available: bool = False
    mcp_available: bool = False
    completion_policy: str = ""


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


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    try:
        return bool(value)
    except Exception:
        return default
