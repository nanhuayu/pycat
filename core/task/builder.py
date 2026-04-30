"""Build RunPolicy from mode slug + feature toggles.

Pure core helper (no Qt) — reusable by CLI/TUI.
"""
from __future__ import annotations

import logging
from typing import Optional

from core.task.types import RetryPolicy, RunPolicy
from core.modes.features import clamp_feature_flags, get_mode_feature_policy
from core.modes.manager import ModeManager, resolve_mode_config
from core.config.schema import RetryConfig


logger = logging.getLogger(__name__)


def build_run_policy(
    *,
    mode_slug: str,
    enable_thinking: Optional[bool] = None,
    enable_search: Optional[bool] = None,
    enable_mcp: Optional[bool] = None,
    mode_manager: Optional[ModeManager] = None,
    retry_config: Optional[RetryConfig] = None,
) -> RunPolicy:
    """Build a RunPolicy from mode + feature toggles.

    - Apply mode feature-policy clamping to tool flags.
    - Determine defaults from mode groups.
    - Return an immutable RunPolicy.
    """
    slug = (mode_slug or "chat").strip() or "chat"

    mode_cfg = resolve_mode_config(slug, mode_manager=mode_manager)

    max_turns = int(getattr(mode_cfg, "max_turns", None) or 20)
    context_window_limit = int(getattr(mode_cfg, "context_window_limit", None) or 100_000)
    auto_compress_enabled = getattr(mode_cfg, "auto_compress_enabled", None)
    tool_allowlist = set(getattr(mode_cfg, "tool_allowlist", ()) or ()) or None
    tool_denylist = set(getattr(mode_cfg, "tool_denylist", ()) or ()) or None

    # Derive defaults from mode feature policy when caller passes None.
    try:
        fp = get_mode_feature_policy(mode_cfg)
        if enable_thinking is None:
            enable_thinking = bool(fp.default_thinking)
        if enable_mcp is None:
            enable_mcp = bool(fp.default_mcp)
        if enable_search is None:
            enable_search = bool(fp.default_search)
    except Exception as exc:
        logger.debug("Failed to derive default feature flags from mode policy: %s", exc)

    if enable_thinking is None:
        enable_thinking = True
    if enable_search is None:
        enable_search = False
    if enable_mcp is None:
        enable_mcp = False

    # Clamp flags according to mode policy.
    try:
        fp = get_mode_feature_policy(mode_cfg)
        enable_thinking, enable_mcp, enable_search = clamp_feature_flags(
            fp,
            enable_thinking=bool(enable_thinking),
            enable_mcp=bool(enable_mcp),
            enable_search=bool(enable_search),
        )
    except Exception as exc:
        logger.debug("Failed to clamp feature flags from mode policy: %s", exc)
        enable_thinking = bool(enable_thinking)
        enable_search = bool(enable_search)
        enable_mcp = bool(enable_mcp)

    retry = RetryPolicy()
    if retry_config is not None:
        retry = RetryPolicy(
            max_retries=retry_config.max_retries,
            base_delay=retry_config.base_delay,
            backoff_factor=retry_config.backoff_factor,
        )

    return RunPolicy(
        mode=str(slug),
        max_turns=int(max_turns),
        context_window_limit=int(context_window_limit),
        enable_thinking=bool(enable_thinking),
        enable_search=bool(enable_search),
        enable_mcp=bool(enable_mcp),
        tool_allowlist=tool_allowlist,
        tool_denylist=tool_denylist,
        retry=retry,
        auto_compress_enabled=auto_compress_enabled,
    )
