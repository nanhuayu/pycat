from __future__ import annotations

import logging
from dataclasses import replace
from typing import Iterable, Mapping

from pycat.models.contracts.config import AgentRuntimeConfig, RetryConfig
from pycat.models.contracts.tooling import (
    FilesystemScope,
    ToolPermissionConfig,
    ToolSelectionPolicy,
    filesystem_scope_for_mode,
    normalize_tool_category,
    permission_config_for_approval,
)
from pycat.core.modes.manager import ModeManager, resolve_mode_config
from pycat.models.contracts.agent import (
    RetryPolicy,
    RunPolicy,
    effective_pycat_assistant_enabled,
)
from pycat.core.agent.run.control import effective_run_policy
from pycat.models.conversation import Conversation

logger = logging.getLogger(__name__)


def _settings_dict(value: Mapping | None) -> dict:
    return dict(value or {}) if isinstance(value, Mapping) else {}


def _load_retry_config(app_settings: Mapping | None) -> RetryConfig | None:
    raw_retry = _settings_dict(app_settings).get("retry")
    if not isinstance(raw_retry, Mapping):
        return None
    try:
        return RetryConfig.from_dict(raw_retry)
    except Exception as exc:
        logger.debug("Failed to load retry config: %s", exc)
        return None


def _load_tool_permissions(app_settings: Mapping | None) -> ToolPermissionConfig:
    try:
        return ToolPermissionConfig.from_settings_dict(_settings_dict(app_settings))
    except Exception as exc:
        logger.debug("Failed to load tool permissions: %s", exc)
    return ToolPermissionConfig()


def _load_agent_runtime_config(app_settings: Mapping | None) -> AgentRuntimeConfig:
    raw_agent = _settings_dict(app_settings).get("agent")
    if isinstance(raw_agent, Mapping):
        try:
            return AgentRuntimeConfig.from_dict(raw_agent)
        except Exception as exc:
            logger.debug("Failed to load agent runtime config: %s", exc)
    return AgentRuntimeConfig()


def _conversation_tool_selection(conversation: Conversation) -> ToolSelectionPolicy | None:
    settings = getattr(conversation, "settings", {}) or {}
    raw_tool_selection = settings.get("tool_selection") if isinstance(settings, Mapping) else None
    if not isinstance(raw_tool_selection, Mapping):
        return None
    try:
        return ToolSelectionPolicy.from_dict(raw_tool_selection)
    except Exception as exc:
        logger.debug("Failed to load conversation tool selection: %s", exc)
        return None


def build_run_policy(
    *,
    mode_slug: str,
    reasoning_mode: str | None = None,
    show_thinking: bool = True,
    pycat_assistant_enabled: bool | None = None,
    tool_selection: ToolSelectionPolicy | None = None,
    mode_manager: ModeManager | None = None,
    retry_config: RetryConfig | None = None,
    tool_permissions: ToolPermissionConfig | None = None,
    filesystem_scope: FilesystemScope | None = None,
) -> RunPolicy:
    """Build a RunPolicy baseline from mode + a canonical ToolSelectionPolicy.

    Pure core helper (no Qt) — reusable by GUI/CLI/Channel entry points.
    """
    slug = (mode_slug or "chat").strip() or "chat"

    mode_cfg = resolve_mode_config(slug, mode_manager=mode_manager)

    max_turns = int(getattr(mode_cfg, "max_turns", None) or 200)
    auto_compress_enabled = None
    allowed_tool_categories = {
        normalize_tool_category(category)
        for category in mode_cfg.tool_category_names()
        if str(category or "").strip()
    }

    normalized_reasoning_mode = str(reasoning_mode or "").strip().lower() or None
    effective_permissions = tool_permissions or ToolPermissionConfig()
    effective_assistant = effective_pycat_assistant_enabled(
        pycat_assistant_enabled,
        mode=slug,
    )
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
        reasoning_mode=normalized_reasoning_mode,
        show_thinking=bool(show_thinking),
        pycat_assistant_enabled=effective_assistant,
        completion_policy=str(getattr(mode_cfg, "completion_policy", "text") or "text"),
        tool_selection=tool_selection,
        tool_permissions=effective_permissions,
        filesystem_scope=filesystem_scope or FilesystemScope(),
        retry=retry,
        auto_compress_enabled=auto_compress_enabled,
    )


class RunPolicyBuilder:
    """Build the single RunPolicy used by every runtime entry point."""

    @staticmethod
    def build(
        *,
        conversation: Conversation,
        app_settings: Mapping | None = None,
        mode_slug: str | None = None,
        reasoning_mode: str | None = None,
        show_thinking: bool | None = None,
        work_dir: str | None = None,
        mode_manager: ModeManager | None = None,
        retry_config: RetryConfig | None = None,
        tool_selection: ToolSelectionPolicy | None = None,
        tool_permissions: ToolPermissionConfig | None = None,
        filesystem_scope: FilesystemScope | None = None,
        denied_tools: Iterable[str] = (),
        source: str = "desktop",
        max_turns: int | None = None,
    ) -> RunPolicy:
        app_settings_dict = _settings_dict(app_settings)
        settings = getattr(conversation, "settings", {}) or {}
        settings = settings if isinstance(settings, Mapping) else {}
        effective_source = str(source or "desktop").strip() or "desktop"

        slug = str(mode_slug or getattr(conversation, "mode", "") or "chat").strip() or "chat"

        # Reasoning defaults are owned by the selected ModelProfile.  A legacy
        # conversation value is accepted once in memory, then removed by
        # Conversation/LLMConfig serialization; internal callers may also
        # provide this short-lived RunPolicy override.
        if reasoning_mode is None:
            raw_llm_config = getattr(conversation, "llm_config", {})
            if isinstance(raw_llm_config, Mapping):
                reasoning_mode = raw_llm_config.get("reasoning_mode") or raw_llm_config.get("reasoning_effort")
        if show_thinking is None:
            show_thinking_default = bool(app_settings_dict.get("show_thinking", True))
            show_thinking = bool(settings.get("show_thinking", show_thinking_default))

        pycat_assistant_enabled = effective_pycat_assistant_enabled(
            settings.get("pycat_assistant_enabled", True),
            mode=slug,
        )

        retry = retry_config or _load_retry_config(app_settings_dict)
        selection = ToolSelectionPolicy.all()
        conversation_selection = _conversation_tool_selection(conversation)
        if conversation_selection is not None:
            selection = selection.intersect(conversation_selection)
        if tool_selection is not None:
            selection = selection.intersect(tool_selection)

        tool_approval = settings.get("tool_approval")
        if tool_permissions is not None:
            base_permissions = tool_permissions
        else:
            base_permissions = permission_config_for_approval(
                tool_approval,
                custom=_load_tool_permissions(app_settings_dict),
            )
        effective_permissions = base_permissions
        effective_filesystem_scope = (
            filesystem_scope
            if filesystem_scope is not None
            else filesystem_scope_for_mode(
                settings.get("filesystem_mode"),
                allow_home_read=effective_source in {"desktop", "cli"},
            )
        )
        fixed_denials = {str(name or "").strip() for name in denied_tools if str(name or "").strip()}
        if fixed_denials:
            selection = selection.intersect(ToolSelectionPolicy(denied_tools=fixed_denials))

        manager = mode_manager or ModeManager(work_dir or getattr(conversation, "work_dir", None) or None, data_dir=getattr(conversation, "data_dir", None))
        policy = build_run_policy(
            mode_slug=slug,
            reasoning_mode=reasoning_mode,
            show_thinking=bool(show_thinking),
            pycat_assistant_enabled=pycat_assistant_enabled,
            tool_selection=selection,
            mode_manager=manager,
            retry_config=retry,
            tool_permissions=effective_permissions,
            filesystem_scope=effective_filesystem_scope,
        )
        policy, _revision = effective_run_policy(policy)
        agent_config = _load_agent_runtime_config(app_settings_dict)
        try:
            mode_cfg = manager.get(slug)
        except Exception:
            mode_cfg = None

        updates: dict[str, object] = {"source": effective_source}
        mode_has_turn_budget = False
        if mode_cfg is not None:
            mode_has_turn_budget = bool(getattr(mode_cfg, "max_turns", None))
        if not mode_has_turn_budget:
            updates["max_turns"] = int(agent_config.max_turns or policy.max_turns)
        if max_turns is not None:
            updates["max_turns"] = min(int(updates.get("max_turns", policy.max_turns)), max(1, int(max_turns)))

        return replace(
            policy,
            **updates,
        )
