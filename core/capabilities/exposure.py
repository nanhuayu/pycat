"""Capability tool registration helpers shared by runtime tools and settings UI."""
from __future__ import annotations

from .defaults import default_capabilities_config
from .types import CapabilitiesConfig, CapabilityConfig


def capability_exposed_as_tool(capability: CapabilityConfig) -> bool:
    """Return whether a capability should become a model-visible tool."""
    return str(getattr(capability, "visibility", "") or "") == "agent_tool"


def exposed_capability_ids(config: CapabilitiesConfig | None = None) -> list[str]:
    cfg = config or default_capabilities_config()
    return [cap.id for cap in cfg.capabilities if capability_exposed_as_tool(cap)]


def capability_tool_ids(config: CapabilitiesConfig | None = None) -> list[str]:
    """Return ids for capabilities that become ``capability__*`` tools."""
    return exposed_capability_ids(config)


def format_capability_list(config: CapabilitiesConfig) -> str:
    items = []
    for cap in config.capabilities:
        if not capability_exposed_as_tool(cap):
            continue
        items.append(f"{cap.id} ({cap.name})")
    return ", ".join(items) or "none"
