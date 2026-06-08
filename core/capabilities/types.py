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

LEGACY_CAPABILITY_ID_ALIASES: dict[str, str] = {
    "context_compress": "compress",
    "title_extract": "title",
    "summarize_text": "summarize",
}

BUILTIN_CAPABILITY_VISIBILITY_DEFAULTS: dict[str, str] = {
    "prompt_optimize": "internal",
    "title": "internal",
    "compress": "internal",
    "translate": "agent_tool",
    "summarize": "agent_tool",
    "extract_facts": "agent_tool",
    "classify_risk": "agent_tool",
    "rewrite_query": "agent_tool",
}

BUILTIN_CAPABILITY_KIND_DEFAULTS: dict[str, str] = {
    "prompt_optimize": "prompt_optimize",
    "title": "title",
    "compress": "compress",
    "translate": "translate",
    "summarize": "summarize",
    "extract_facts": "extract_facts",
    "classify_risk": "classify_risk",
    "rewrite_query": "rewrite_query",
}

@dataclass(frozen=True)
class CapabilityConfig:
    """Reusable LLM task capability definition.

    ``visibility`` controls where the capability can be used:
    - ``internal``: callable by runtime/UI/internal services, not model-visible.
    - ``agent_tool``: callable internally and registered as ``capability__<id>``.
    - ``hidden``: disabled; not callable and not exposed.
    """

    id: str
    name: str
    kind: str = "custom"
    visibility: str = "agent_tool"
    execution_mode: str = "direct_llm"
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
        raw_id = _as_str(payload.get("id") or payload.get("slug"), "").strip()
        capability_id = LEGACY_CAPABILITY_ID_ALIASES.get(raw_id.strip().lower(), raw_id)
        kind = _as_str(payload.get("kind"), "custom").strip().lower() or "custom"
        if not capability_id:
            capability_id = kind
        capability_id = LEGACY_CAPABILITY_ID_ALIASES.get(capability_id.strip().lower(), capability_id)
        kind = LEGACY_CAPABILITY_ID_ALIASES.get(kind, kind)
        visibility = _as_str(payload.get("visibility"), "").strip().lower()
        if visibility not in {"internal", "agent_tool", "hidden"}:
            if "enabled" in payload and not _as_bool(payload.get("enabled"), True):
                visibility = "hidden"
            else:
                visibility = BUILTIN_CAPABILITY_VISIBILITY_DEFAULTS.get(capability_id.lower()) or "agent_tool"
        execution_mode = _as_str(payload.get("execution_mode") or payload.get("executionMode"), "").strip().lower()
        if execution_mode not in {"direct_llm", "tool_limited_loop"}:
            execution_mode = "tool_limited_loop" if _as_list(payload.get("allowed_tool_categories")) else "direct_llm"
        return CapabilityConfig(
            id=capability_id,
            name=_as_str(payload.get("name"), capability_id).strip() or capability_id,
            kind=BUILTIN_CAPABILITY_KIND_DEFAULTS.get(capability_id.lower(), kind),
            visibility=visibility,
            execution_mode=execution_mode,
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

    @property
    def hidden(self) -> bool:
        return self.visibility == "hidden"

    @property
    def exposed_as_tool(self) -> bool:
        return self.visibility == "agent_tool"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "visibility": self.visibility,
            "execution_mode": self.execution_mode,
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
        target = LEGACY_CAPABILITY_ID_ALIASES.get(str(capability_id or "").strip().lower(), str(capability_id or "").strip().lower())
        for item in self.capabilities:
            if item.id.lower() == target:
                return item
        return None


CapabilityDefinition = CapabilityConfig
