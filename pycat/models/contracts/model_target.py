"""Model routing preferences shared by capabilities and delegated agents."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

ModelTargetSource = Literal["primary", "auxiliary", "explicit"]


@dataclass(frozen=True)
class ModelTarget:
    """Select a model by role or by an explicit ``provider|model`` reference."""

    source: ModelTargetSource = "auxiliary"
    model_ref: str = ""

    @staticmethod
    def from_dict(value: Mapping[str, Any] | str | None) -> "ModelTarget":
        if isinstance(value, str):
            model_ref = value.strip()
            return ModelTarget(source="explicit", model_ref=model_ref) if model_ref else ModelTarget()

        payload = dict(value or {}) if isinstance(value, Mapping) else {}
        source = str(payload.get("source") or "auxiliary").strip().lower()
        if source not in {"primary", "auxiliary", "explicit"}:
            source = "auxiliary"
        model_ref = str(payload.get("model_ref") or payload.get("modelRef") or "").strip()
        if source == "explicit" and not model_ref:
            source = "auxiliary"
        return ModelTarget(source=source, model_ref=model_ref if source == "explicit" else "")

    @staticmethod
    def explicit(model_ref: str) -> "ModelTarget":
        value = str(model_ref or "").strip()
        return ModelTarget(source="explicit", model_ref=value) if value else ModelTarget()

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "model_ref": self.model_ref if self.source == "explicit" else "",
        }

    def display_text(self) -> str:
        if self.source == "primary":
            return "跟随会话主模型"
        if self.source == "explicit":
            return self.model_ref or "使用辅助模型默认值"
        return "使用辅助模型默认值"
