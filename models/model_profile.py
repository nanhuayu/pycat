"""Per-model capability profile."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


def _coerce_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = int(value)
        return number if number > 0 else None
    except Exception:
        return None


def _coerce_optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _coerce_bool(value: Any, default: bool = False) -> bool:
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


@dataclass
class ModelProfile:
    """Lightweight per-model capability profile.

    The project still keeps ``Provider.models`` as the legacy/simple model list.
    ``ModelProfile`` lets new code attach capabilities without forcing a full
    migration of existing provider settings.
    """

    model_id: str = ""
    display_name: str = ""
    context_window: int | None = None
    max_output_tokens: int | None = None
    supports_tools: bool = True
    supports_vision: bool = True
    supports_reasoning: bool = False
    reasoning_style: str = "none"
    default_temperature: float | None = None
    default_top_p: float | None = None
    reasoning_effort: str = ""
    thinking_budget_tokens: int | None = None
    reasoning_config: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    notes: str = ""

    def __post_init__(self) -> None:
        self.model_id = str(self.model_id or "").strip()
        self.display_name = str(self.display_name or "").strip()
        self.context_window = _coerce_optional_int(self.context_window)
        self.max_output_tokens = _coerce_optional_int(self.max_output_tokens)
        self.supports_tools = _coerce_bool(self.supports_tools, True)
        self.supports_vision = _coerce_bool(self.supports_vision, True)
        self.supports_reasoning = _coerce_bool(self.supports_reasoning, False)
        self.reasoning_style = str(self.reasoning_style or "none").strip().lower() or "none"
        self.default_temperature = _coerce_optional_float(self.default_temperature)
        self.default_top_p = _coerce_optional_float(self.default_top_p)
        self.reasoning_effort = str(self.reasoning_effort or "").strip().lower()
        self.thinking_budget_tokens = _coerce_optional_int(self.thinking_budget_tokens)
        self.reasoning_config = dict(self.reasoning_config or {}) if isinstance(self.reasoning_config, dict) else {}
        self.tags = [str(tag).strip() for tag in (self.tags or []) if str(tag).strip()]
        self.notes = str(self.notes or "").strip()

    @classmethod
    def from_model_id(
        cls,
        model_id: str,
        *,
        supports_vision: bool = True,
        supports_reasoning: bool = False,
    ) -> "ModelProfile":
        return cls(
            model_id=model_id,
            display_name=model_id,
            supports_vision=supports_vision,
            supports_reasoning=supports_reasoning,
            reasoning_style="reasoning" if supports_reasoning else "none",
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any] | str | None) -> "ModelProfile":
        if isinstance(data, str):
            return cls.from_model_id(data)
        payload = dict(data or {})
        return cls(
            model_id=payload.get("model_id") or payload.get("id") or payload.get("name") or "",
            display_name=payload.get("display_name") or payload.get("label") or "",
            context_window=payload.get("context_window"),
            max_output_tokens=payload.get("max_output_tokens"),
            supports_tools=payload.get("supports_tools", True),
            supports_vision=payload.get("supports_vision", True),
            supports_reasoning=payload.get("supports_reasoning", False),
            reasoning_style=payload.get("reasoning_style") or payload.get("reasoning_type") or "none",
            default_temperature=payload.get("default_temperature"),
            default_top_p=payload.get("default_top_p"),
            reasoning_effort=payload.get("reasoning_effort") or payload.get("effort") or "",
            thinking_budget_tokens=payload.get("thinking_budget_tokens") or payload.get("thinking_budget"),
            reasoning_config=payload.get("reasoning_config") or {},
            tags=payload.get("tags") or [],
            notes=payload.get("notes") or "",
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "display_name": self.display_name,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "supports_tools": self.supports_tools,
            "supports_vision": self.supports_vision,
            "supports_reasoning": self.supports_reasoning,
            "reasoning_style": self.reasoning_style,
            "default_temperature": self.default_temperature,
            "default_top_p": self.default_top_p,
            "reasoning_effort": self.reasoning_effort,
            "thinking_budget_tokens": self.thinking_budget_tokens,
            "reasoning_config": dict(self.reasoning_config),
            "tags": list(self.tags),
            "notes": self.notes,
        }
