from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from .defaults import default_capabilities_config
from .types import CapabilitiesConfig, CapabilityConfig


class CapabilitiesManager:
    """Loads built-in and user-defined capabilities.

    The manager is deliberately small: it only merges configuration and offers
    lookups. Runtime execution remains owned by prompt optimizer, context
    condenser, task executor, or future capability runners.
    """

    def __init__(self, config_path: str | Path | None = None) -> None:
        self.config_path = Path(config_path) if config_path else None
        self._config = default_capabilities_config()

    @property
    def config(self) -> CapabilitiesConfig:
        return self._config

    def load(self) -> CapabilitiesConfig:
        config = default_capabilities_config()
        if self.config_path and self.config_path.exists():
            try:
                payload = json.loads(self.config_path.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
            if isinstance(payload, Mapping):
                config = self.merge(config, CapabilitiesConfig.from_dict(payload))
        self._config = config
        return config

    def save(self, config: CapabilitiesConfig | None = None) -> None:
        if not self.config_path:
            return
        target = config or self._config
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(target.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._config = target

    def capability(self, capability_id: str) -> CapabilityConfig | None:
        return self._config.capability(capability_id)

    @staticmethod
    def merge(base: CapabilitiesConfig, override: CapabilitiesConfig) -> CapabilitiesConfig:
        capabilities: dict[str, CapabilityConfig] = {item.id: item for item in base.capabilities}
        for item in override.capabilities:
            base_item = capabilities.get(item.id)
            if base_item is None:
                capabilities[item.id] = item
                continue
            capabilities[item.id] = CapabilityConfig(
                id=item.id or base_item.id,
                name=item.name or base_item.name,
                kind=item.kind or base_item.kind,
                enabled=item.enabled,
                model_ref=item.model_ref or base_item.model_ref,
                mode=item.mode or base_item.mode,
                system_prompt=item.system_prompt or base_item.system_prompt,
                description=item.description or base_item.description,
                tool_groups=item.tool_groups or base_item.tool_groups,
                input_schema=item.input_schema or base_item.input_schema,
                output_schema=item.output_schema or base_item.output_schema,
                options={**(base_item.options or {}), **(item.options or {})},
            )

        return CapabilitiesConfig(capabilities=tuple(capabilities.values()))
