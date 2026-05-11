"""Build RunPolicy from mode slug + tool selection.

Pure core helper (no Qt) — reusable by CLI/TUI.
"""
from __future__ import annotations

from typing import Optional

from core.config.schema import ToolPermissionConfig
from core.task.types import RetryPolicy, RunPolicy
from core.tools.catalog import ToolSelectionPolicy, normalize_tool_category
from core.modes.manager import ModeManager, resolve_mode_config
from core.config.schema import RetryConfig

def build_run_policy(
    *,
    mode_slug: str,
    enable_thinking: Optional[bool] = None,
    tool_selection: Optional[ToolSelectionPolicy] = None,
    mode_manager: Optional[ModeManager] = None,
    retry_config: Optional[RetryConfig] = None,
    tool_permissions: Optional["ToolPermissionConfig"] = None,
) -> RunPolicy:
    """Build a RunPolicy from mode + a canonical ToolSelectionPolicy."""
    slug = (mode_slug or "chat").strip() or "chat"

    mode_cfg = resolve_mode_config(slug, mode_manager=mode_manager)

    max_turns = int(getattr(mode_cfg, "max_turns", None) or 200)
    context_window_limit = int(getattr(mode_cfg, "context_window_limit", None) or 100_000)
    auto_compress_enabled = getattr(mode_cfg, "auto_compress_enabled", None)
    allowed_tool_categories = {
        normalize_tool_category(category)
        for category in mode_cfg.tool_category_names()
        if str(category or "").strip()
    } or None

    if enable_thinking is None:
        enable_thinking = bool({"edit", "execute"}.intersection(allowed_tool_categories or set()))

    effective_permissions = tool_permissions or ToolPermissionConfig()
    if tool_selection is None:
        tool_selection = ToolSelectionPolicy.from_categories(allowed_tool_categories)

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
        tool_selection=tool_selection,
        tool_permissions=effective_permissions,
        retry=retry,
        auto_compress_enabled=auto_compress_enabled,
    )
