"""Reusable single-turn and Agent-loop capability contracts."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Mapping

from pycat.models.contracts.model_target import ModelTarget
from pycat.models.contracts.tooling import normalize_tool_category


CAPABILITIES_SCHEMA_VERSION = 6


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class ImageGenerationOptions:
    size: str = "auto"
    quality: str = "auto"
    output_format: str = "auto"
    background: str = "auto"
    n: int = 1

    def __post_init__(self) -> None:
        if self.size not in {"auto", "1K", "2K", "4K"} and not re.fullmatch(r"[1-9][0-9]{1,4}x[1-9][0-9]{1,4}", self.size):
            raise ValueError("图片尺寸应为 auto 或 宽x高，例如 1536x1024。")
        if self.quality not in {"auto", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("图片质量无效。")
        if self.output_format not in {"auto", "png", "jpeg", "webp"}:
            raise ValueError("图片格式应为 PNG、JPEG 或 WebP。")
        if self.background not in {"auto", "opaque", "transparent"}:
            raise ValueError("图片背景设置无效。")
        if self.background == "transparent" and self.output_format == "jpeg":
            raise ValueError("透明背景需要 PNG 或 WebP 格式。")
        if not isinstance(self.n, int) or isinstance(self.n, bool) or not 1 <= self.n <= 4:
            raise ValueError("每次生成数量应为 1 到 4 张。")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "ImageGenerationOptions":
        payload = _mapping(data)
        return cls(**{key: payload[key] for key in ("size", "quality", "output_format", "background", "n") if key in payload})

    def to_dict(self) -> dict[str, Any]:
        return {"size": self.size, "quality": self.quality, "output_format": self.output_format,
                "background": self.background, "n": self.n}


@dataclass(frozen=True)
class CapabilityConfig:
    id: str
    name: str
    enabled: bool = True
    exposure: str = "tool"
    runtime: str = "single_turn"
    operation: str = "text"
    image_options: ImageGenerationOptions = field(default_factory=ImageGenerationOptions)
    model_target: ModelTarget = field(default_factory=ModelTarget)
    description: str = ""
    prompt: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    allowed_tool_categories: tuple[str, ...] = ()
    max_turns: int | None = None
    temperature: float | None = None
    max_tokens: int | None = None

    def __post_init__(self) -> None:
        exposure = str(self.exposure or "tool").strip().lower()
        object.__setattr__(self, "exposure", exposure if exposure in {"internal", "tool"} else "tool")
        runtime = str(self.runtime or "single_turn").strip().lower()
        if runtime not in {"single_turn", "agent_loop"}:
            runtime = "single_turn"
        object.__setattr__(self, "runtime", runtime)
        if self.operation not in {"text", "image"}:
            raise ValueError("Capability operation must be text or image")
        if self.operation == "image" and runtime != "single_turn":
            raise ValueError("图像能力只支持单次执行。")
        categories: list[str] = []
        for raw in self.allowed_tool_categories or ():
            category = normalize_tool_category(raw)
            if category not in categories:
                categories.append(category)
        object.__setattr__(self, "allowed_tool_categories", tuple(categories))
        try:
            max_turns = int(self.max_turns) if self.max_turns is not None else None
        except Exception:
            max_turns = None
        object.__setattr__(self, "max_turns", max_turns if max_turns and max_turns > 0 else None)
        try:
            temperature = float(self.temperature) if self.temperature is not None else None
        except Exception:
            temperature = None
        object.__setattr__(self, "temperature", temperature)
        try:
            max_tokens = int(self.max_tokens) if self.max_tokens is not None else None
        except Exception:
            max_tokens = None
        object.__setattr__(self, "max_tokens", max_tokens if max_tokens and max_tokens > 0 else None)

    @staticmethod
    def from_dict(data: Mapping[str, Any] | None) -> "CapabilityConfig":
        payload = _mapping(data)
        capability_id = str(payload.get("id") or "").strip().lower()
        exposure = str(payload.get("exposure") or "").strip().lower()
        if exposure not in {"internal", "tool"}:
            exposure = "tool"
        runtime = str(payload.get("runtime") or "single_turn").strip().lower()
        return CapabilityConfig(
            id=capability_id,
            name=str(payload.get("name") or capability_id).strip() or capability_id,
            enabled=bool(payload.get("enabled", True)),
            exposure=exposure,
            runtime=runtime,
            operation=str(payload.get("operation") or "text"),
            image_options=ImageGenerationOptions.from_dict(payload.get("image_options")),
            model_target=ModelTarget.from_dict(payload.get("model_target")),
            description=str(payload.get("description") or "").strip(),
            prompt=str(payload.get("prompt") or "").strip(),
            input_schema=_mapping(payload.get("input_schema")),
            output_schema=_mapping(payload.get("output_schema")),
            allowed_tool_categories=tuple(payload.get("allowed_tool_categories") or ()),
            max_turns=payload.get("max_turns"),
            temperature=payload.get("temperature"),
            max_tokens=payload.get("max_tokens"),
        )

    @property
    def exposed_as_tool(self) -> bool:
        return self.enabled and self.exposure == "tool"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": bool(self.enabled),
            "exposure": self.exposure,
            "runtime": self.runtime,
            "operation": self.operation,
            "image_options": self.image_options.to_dict(),
            "model_target": self.model_target.to_dict(),
            "description": self.description,
            "prompt": self.prompt,
            "input_schema": dict(self.input_schema or {}),
            "output_schema": dict(self.output_schema or {}),
            "allowed_tool_categories": list(self.allowed_tool_categories),
            "max_turns": self.max_turns,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }


@dataclass(frozen=True)
class CapabilitiesConfig:
    capabilities: tuple[CapabilityConfig, ...] = ()
    schema_version: int = CAPABILITIES_SCHEMA_VERSION

    @staticmethod
    def from_dict(data: Mapping[str, Any] | None) -> "CapabilitiesConfig":
        payload = _mapping(data)
        items = payload.get("capabilities") if isinstance(payload.get("capabilities"), list) else []
        capabilities = [CapabilityConfig.from_dict(item) for item in items if isinstance(item, Mapping)]
        return CapabilitiesConfig(capabilities=tuple(item for item in capabilities if item.id))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CAPABILITIES_SCHEMA_VERSION,
            "capabilities": [item.to_dict() for item in self.capabilities],
        }

    def capability(self, capability_id: str) -> CapabilityConfig | None:
        target = str(capability_id or "").strip().lower()
        return next((item for item in self.capabilities if item.id.lower() == target), None)


CapabilityDefinition = CapabilityConfig
