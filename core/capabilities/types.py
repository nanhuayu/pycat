from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    try:
        return str(value)
    except Exception:
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    try:
        return bool(value)
    except Exception:
        return default


REMOVED_BUILTIN_CAPABILITY_IDS: frozenset[str] = frozenset(
    {
        "context_curator",
        "image_generate",
        "research_brief",
        "researcher",
        "summarize_long_text",
        "tool_result_analyzer",
    }
)

@dataclass(frozen=True)
class CapabilityConfig:
    """Reusable task capability configuration.

    A capability is a self-contained workflow exposed as a tool (``capability__*``).
    It carries its own instructions, model, tool categories, and execution budget.
    There is no separate configured sub-agent entity; each capability is the
    first-class reusable runtime entity.
    """

    id: str
    name: str
    kind: str = "custom"
    enabled: bool = True
    model_ref: str = ""
    system_prompt: str = ""
    description: str = ""
    allowed_tool_categories: tuple[str, ...] = ()
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_dict(data: Mapping[str, Any] | None) -> "CapabilityConfig":
        payload = _as_dict(data)
        capability_id = _as_str(payload.get("id") or payload.get("slug"), "").strip()
        kind = _as_str(payload.get("kind"), "custom").strip().lower() or "custom"
        if not capability_id:
            capability_id = kind
        return CapabilityConfig(
            id=capability_id,
            name=_as_str(payload.get("name"), capability_id).strip() or capability_id,
            kind=kind,
            enabled=_as_bool(payload.get("enabled"), True),
            model_ref=_as_str(payload.get("model_ref") or payload.get("modelRef"), "").strip(),
            system_prompt=_as_str(payload.get("system_prompt") or payload.get("systemPrompt"), "").strip(),
            description=_as_str(payload.get("description") or payload.get("desc"), "").strip(),
            allowed_tool_categories=tuple(
                _as_str(item, "").strip()
                for item in _as_list(payload.get("allowed_tool_categories"))
                if _as_str(item, "").strip()
            ),
            input_schema=_as_dict(payload.get("input_schema") or payload.get("inputSchema")),
            output_schema=_as_dict(payload.get("output_schema") or payload.get("outputSchema")),
            options=_as_dict(payload.get("options")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "enabled": bool(self.enabled),
            "model_ref": self.model_ref,
            "system_prompt": self.system_prompt,
            "description": self.description,
            "allowed_tool_categories": list(self.allowed_tool_categories),
            "input_schema": dict(self.input_schema or {}),
            "output_schema": dict(self.output_schema or {}),
            "options": dict(self.options or {}),
        }

@dataclass(frozen=True)
class CapabilitiesConfig:
    capabilities: tuple[CapabilityConfig, ...] = ()

    @staticmethod
    def from_dict(data: Mapping[str, Any] | None) -> "CapabilitiesConfig":
        payload = _as_dict(data)
        capabilities: list[CapabilityConfig] = []
        for item in _as_list(payload.get("capabilities")):
            if isinstance(item, Mapping):
                capability = CapabilityConfig.from_dict(item)
                if capability.id.strip().lower() in REMOVED_BUILTIN_CAPABILITY_IDS:
                    continue
                capabilities.append(capability)
        return CapabilitiesConfig(capabilities=tuple(capabilities))

    def to_dict(self) -> dict[str, Any]:
        return {
            "capabilities": [item.to_dict() for item in self.capabilities],
        }

    def capability(self, capability_id: str) -> CapabilityConfig | None:
        target = str(capability_id or "").strip().lower()
        for item in self.capabilities:
            if item.id.lower() == target:
                return item
        return None
