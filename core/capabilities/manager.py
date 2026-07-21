"""Load, merge, and persist compact Capability definitions."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from collections.abc import Mapping

from .defaults import default_capabilities_config
from models.contracts.capability import CapabilitiesConfig, CapabilityConfig
from core.config.migrations import migrate_capabilities_payload


def merge_capability(base: CapabilityConfig, override: CapabilityConfig) -> CapabilityConfig:
    return replace(
        base,
        name=override.name or base.name,
        enabled=override.enabled,
        exposure=override.exposure or base.exposure,
        runtime=override.runtime or base.runtime,
        model_target=override.model_target if override.model_target.model_ref else base.model_target,
        description=override.description or base.description,
        prompt=override.prompt or base.prompt,
        input_schema=override.input_schema or base.input_schema,
        output_schema=override.output_schema or base.output_schema,
        allowed_tool_categories=override.allowed_tool_categories or base.allowed_tool_categories,
        max_turns=override.max_turns if override.max_turns is not None else base.max_turns,
    )


class CapabilitiesManager:
    def __init__(self, config_path: str | Path | None = None) -> None:
        self.config_path = Path(config_path) if config_path else None
        self._config = default_capabilities_config()
        self.migrated_profiles: list[dict] = []

    @property
    def config(self) -> CapabilitiesConfig:
        return self._config

    def load(self) -> CapabilitiesConfig:
        config = default_capabilities_config()
        self.migrated_profiles = []
        if self.config_path and self.config_path.exists():
            try:
                raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            except Exception:
                raw = {}
            if isinstance(raw, Mapping):
                migrated, profiles = migrate_capabilities_payload(raw)
                self.migrated_profiles = profiles
                if raw != migrated:
                    self._write(migrated)
                config = self.merge(config, CapabilitiesConfig.from_dict(migrated))
        self._config = config
        return config

    def save(self, config: CapabilitiesConfig | None = None) -> None:
        target = config or self._config
        if self.config_path:
            self._write(target.to_dict())
        self._config = target

    def capability(self, capability_id: str) -> CapabilityConfig | None:
        return self._config.capability(capability_id)

    @staticmethod
    def merge(base: CapabilitiesConfig, override: CapabilitiesConfig) -> CapabilitiesConfig:
        values = {item.id.lower(): item for item in base.capabilities}
        for item in override.capabilities:
            current = values.get(item.id.lower())
            values[item.id.lower()] = merge_capability(current, item) if current else item
        return CapabilitiesConfig(capabilities=tuple(values.values()))

    def _write(self, payload: dict) -> None:
        if not self.config_path:
            return
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.config_path.name}.", suffix=".tmp", dir=str(self.config_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temp_name, self.config_path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
