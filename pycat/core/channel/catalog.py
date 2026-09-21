from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, Protocol

from pycat.models.contracts.channel import ChannelConfig


@dataclass(frozen=True)
class ChannelFieldDefinition:
    key: str
    label: str
    placeholder: str = ""
    help_text: str = ""
    required: bool = False
    secret: bool = False
    show_for_modes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChannelDefinition:
    type: str
    name: str
    description: str
    icon_name: str
    default_name: str
    default_config: Dict[str, Any] = field(default_factory=dict)
    fields: tuple[ChannelFieldDefinition, ...] = ()
    summary_keys: tuple[str, ...] = ()
    featured: bool = True
    tags: tuple[str, ...] = ()

    def normalize_source(self, name: str = "") -> str:
        raw_name = str(name or self.default_name or self.name or self.type).strip()
        safe = raw_name.lower().replace(" ", "-") or self.type
        return f"channel:{self.type}:{safe}"

    def apply_defaults(self, channel: ChannelConfig) -> ChannelConfig:
        merged_config = dict(self.default_config or {})
        merged_config.update(getattr(channel, "config", {}) or {})
        normalized_name = str(getattr(channel, "name", "") or "").strip() or self.default_name or self.name
        normalized_source = str(getattr(channel, "source", "") or "").strip() or self.normalize_source(normalized_name)
        return replace(
            channel,
            type=self.type,
            name=normalized_name,
            source=normalized_source,
            config=merged_config,
        )


@dataclass(frozen=True)
class ChannelInstance:
    definition: ChannelDefinition
    config: ChannelConfig
    summary: str = ""
    validation_errors: tuple[str, ...] = ()

    @property
    def title(self) -> str:
        return str(self.config.name or self.definition.default_name or self.definition.name or self.definition.type).strip()

    @property
    def status_label(self) -> str:
        if not self.config.enabled:
            return "已停用"
        if self.validation_errors:
            return "配置不完整"
        return "待应用"


class ChannelDefinitionAdapter(Protocol):
    definition: ChannelDefinition

    def normalize(self, channel: ChannelConfig) -> ChannelConfig:
        ...

    def validate(self, channel: ChannelConfig) -> tuple[str, ...]:
        ...

    def summarize(self, channel: ChannelConfig) -> str:
        ...


class DeclarativeChannelDefinition:
    definition: ChannelDefinition

    def __init__(self, definition: ChannelDefinition) -> None:
        self.definition = definition

    def normalize(self, channel: ChannelConfig) -> ChannelConfig:
        return self.definition.apply_defaults(channel)

    def validate(self, channel: ChannelConfig) -> tuple[str, ...]:
        normalized = self.normalize(channel)
        config = dict(getattr(normalized, "config", {}) or {})
        current_mode = str(config.get("connection_mode", "") or "").strip().lower()
        issues: list[str] = []
        for field_def in self.definition.fields:
            if field_def.show_for_modes and current_mode and current_mode not in set(field_def.show_for_modes):
                continue
            if not field_def.required:
                continue
            if str(config.get(field_def.key, "") or "").strip():
                continue
            issues.append(f"缺少必填项：{field_def.label}")
        return tuple(issues)

    def summarize(self, channel: ChannelConfig) -> str:
        normalized = self.normalize(channel)
        config = dict(getattr(normalized, "config", {}) or {})
        parts: list[str] = []
        field_map = {field_def.key: field_def for field_def in self.definition.fields}
        for key in self.definition.summary_keys:
            value = str(config.get(key, "") or "").strip()
            if not value:
                continue
            label = field_map.get(key).label if key in field_map else key
            parts.append(f"{label}: {value}")
        if not parts and str(normalized.source or "").strip():
            parts.append(normalized.source)
        return " · ".join(parts)


class ChannelCatalog:
    def __init__(self, definitions: Iterable[ChannelDefinitionAdapter] | None = None) -> None:
        self._definitions: dict[str, ChannelDefinitionAdapter] = {}
        for definition in definitions or ():
            self.register(definition)

    def register(self, definition: ChannelDefinitionAdapter) -> None:
        channel_type = str(definition.definition.type or "").strip().lower()
        if not channel_type:
            raise ValueError("channel definition requires a non-empty type")
        self._definitions[channel_type] = definition

    def adapters(self) -> tuple[ChannelDefinitionAdapter, ...]:
        return tuple(self._definitions.values())

    def definitions(self, *, featured_only: bool = False) -> tuple[ChannelDefinition, ...]:
        definitions = [adapter.definition for adapter in self._definitions.values()]
        if featured_only:
            definitions = [definition for definition in definitions if definition.featured]
        return tuple(definitions)

    def get_adapter(self, channel_type: str) -> ChannelDefinitionAdapter:
        key = str(channel_type or "").strip().lower()
        if not key:
            raise ValueError("channel type is required")
        adapter = self._definitions.get(key)
        if adapter is None:
            raise ValueError(f"unknown channel type: {channel_type}")
        return adapter

    def get_definition(self, channel_type: str) -> ChannelDefinition:
        return self.get_adapter(channel_type).definition

    def ensure_channel(self, channel: ChannelConfig) -> ChannelConfig:
        return self.get_adapter(getattr(channel, "type", "")).normalize(channel)

    def build_instance(self, channel: ChannelConfig) -> ChannelInstance:
        normalized = self.ensure_channel(channel)
        adapter = self.get_adapter(normalized.type)
        return ChannelInstance(
            definition=adapter.definition,
            config=normalized,
            summary=adapter.summarize(normalized),
            validation_errors=adapter.validate(normalized),
        )

    def validate(self, channel: ChannelConfig) -> tuple[str, ...]:
        return self.build_instance(channel).validation_errors

    def summarize(self, channel: ChannelConfig) -> str:
        return self.build_instance(channel).summary

    def featured_types(self) -> tuple[str, ...]:
        return tuple(definition.type for definition in self.definitions(featured_only=True))


def build_channel_catalog(definitions: Iterable[ChannelDefinitionAdapter]) -> ChannelCatalog:
    return ChannelCatalog(definitions)


__all__ = [
    "ChannelCatalog",
    "ChannelDefinition",
    "ChannelDefinitionAdapter",
    "ChannelFieldDefinition",
    "ChannelInstance",
    "DeclarativeChannelDefinition",
    "build_channel_catalog",
]
