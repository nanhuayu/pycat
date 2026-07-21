"""Build RunPolicy from mode slug + tool selection.

Pure core helper (no Qt) — reusable by CLI/TUI.
"""
from __future__ import annotations

from typing import Optional

from models.contracts.tooling import ToolPermissionConfig
from models.contracts.agent import RetryPolicy, RunPolicy
from models.contracts.tooling import ToolSelectionPolicy, normalize_tool_category
from core.modes.manager import ModeManager, resolve_mode_config
from models.contracts.config import RetryConfig

def build_run_policy(
    *,
    mode_slug: str,
    reasoning_enabled: Optional[bool] = None,
    reasoning_effort: str = "",
    show_thinking: bool = True,
    tool_selection: Optional[ToolSelectionPolicy] = None,
    mode_manager: Optional[ModeManager] = None,
    retry_config: Optional[RetryConfig] = None,
    tool_permissions: Optional["ToolPermissionConfig"] = None,
) -> RunPolicy:
    """Build a RunPolicy from mode + a canonical ToolSelectionPolicy."""
    slug = (mode_slug or "chat").strip() or "chat"

    mode_cfg = resolve_mode_config(slug, mode_manager=mode_manager)

    max_turns = int(getattr(mode_cfg, "max_turns", None) or 200)
    context_window_limit = 100_000
    auto_compress_enabled = None
    allowed_tool_categories = {
        normalize_tool_category(category)
        for category in mode_cfg.tool_category_names()
        if str(category or "").strip()
    }

    effort = str(reasoning_effort or "").strip().lower()
    if reasoning_enabled is False and effort:
        raise ValueError("reasoning_effort must be empty when reasoning is disabled")

    effective_permissions = tool_permissions or ToolPermissionConfig()
    mode_selection = ToolSelectionPolicy.from_categories(allowed_tool_categories)
    tool_selection = mode_selection.intersect(tool_selection or ToolSelectionPolicy.all())

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
        reasoning_enabled=reasoning_enabled,
        reasoning_effort=effort,
        show_thinking=bool(show_thinking),
        completion_policy=str(getattr(mode_cfg, "completion_policy", "text") or "text"),
        tool_selection=tool_selection,
        tool_permissions=effective_permissions,
        retry=retry,
        auto_compress_enabled=auto_compress_enabled,
    )
