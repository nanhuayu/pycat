from __future__ import annotations

from dataclasses import dataclass

from core.modes.types import ModeConfig


@dataclass(frozen=True)
class ModeFeaturePolicy:
    """Mode-driven feature defaults and constraints.

    Used by:
    - UI: enable/disable toggles + compute defaults
    - policy_builder: derive RunPolicy feature flags
    """

    allow_thinking: bool = True
    allow_mcp: bool = False
    allow_search: bool = False

    default_thinking: bool = False
    default_mcp: bool = False
    default_search: bool = False


def get_mode_feature_policy(mode: ModeConfig) -> ModeFeaturePolicy:
    """Derive UI + runtime feature policy from ModeConfig.groups."""

    groups = mode.group_names()
    slug = str(getattr(mode, "slug", "") or "").strip().lower()

    allow_mcp = bool("mcp" in groups)
    allow_search = bool("search" in groups)

    default_mcp = bool("mcp" in groups or "command" in groups or "edit" in groups)
    default_search = bool("search" in groups)
    default_thinking = bool("edit" in groups or "command" in groups)

    if slug == "chat":
        default_mcp = False
        default_search = False

    return ModeFeaturePolicy(
        allow_thinking=True,
        allow_mcp=allow_mcp,
        allow_search=allow_search,
        default_thinking=default_thinking,
        default_mcp=default_mcp if allow_mcp else False,
        default_search=default_search if allow_search else False,
    )


def clamp_feature_flags(
    policy: ModeFeaturePolicy,
    *,
    enable_thinking: bool,
    enable_mcp: bool,
    enable_search: bool,
) -> tuple[bool, bool, bool]:
    """Clamp user/UI flags by policy (disallowed → forced False)."""
    out_thinking = bool(enable_thinking) if policy.allow_thinking else False
    out_mcp = bool(enable_mcp) if policy.allow_mcp else False
    out_search = bool(enable_search) if policy.allow_search else False
    return out_thinking, out_mcp, out_search
