"""Capability tool registration helpers shared by runtime tools and settings UI."""
from __future__ import annotations

from pycat.models.contracts.capability import CapabilitiesConfig, CapabilityConfig


def capability_exposed_as_tool(capability: CapabilityConfig) -> bool:
    """Return whether a capability should become a model-visible tool."""
    return bool(getattr(capability, "exposed_as_tool", False))


def format_capability_list(config: CapabilitiesConfig) -> str:
    items = []
    for cap in config.capabilities:
        if not capability_exposed_as_tool(cap):
            continue
        items.append(f"{cap.id} ({cap.name})")
    return ", ".join(items) or "none"
