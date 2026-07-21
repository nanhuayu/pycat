from __future__ import annotations

import logging
from dataclasses import replace
from typing import Iterable, Mapping

from models.contracts.config import AgentRuntimeConfig, RetryConfig
from models.contracts.tooling import ToolPermissionConfig, ToolPolicy
from core.modes.manager import ModeManager
from core.agent.run.policy_builder import build_run_policy
from models.contracts.agent import RunPolicy
from models.contracts.tooling import ToolSelectionPolicy
from models.conversation import Conversation
from models.llm_config import LLMConfig

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


def _merge_tool_permissions(*configs: ToolPermissionConfig | None) -> ToolPermissionConfig:
    values = [config for config in configs if config is not None]
    if not values:
        return ToolPermissionConfig()
    categories = set().union(*(config.category_defaults.keys() for config in values))
    category_defaults: dict[str, ToolPolicy] = {}
    for category in categories:
        policies = [config.category_defaults.get(category, ToolPolicy()) for config in values]
        category_defaults[category] = ToolPolicy(
            enabled=all(item.enabled for item in policies),
            auto_approve=all(item.auto_approve for item in policies),
        )
    tool_names = set().union(*(config.tools.keys() for config in values))
    tools: dict[str, ToolPolicy] = {}
    for name in tool_names:
        policies = [config.tools[name] for config in values if name in config.tools]
        tools[name] = ToolPolicy(
            enabled=all(item.enabled for item in policies),
            auto_approve=all(item.auto_approve for item in policies),
        )
    modes = [str(config.approval_mode or "standard") for config in values]
    if all(mode == "allow_all" for mode in modes):
        approval_mode = "allow_all"
    elif all(mode in {"allow_all", "developer_trust"} for mode in modes):
        approval_mode = "developer_trust"
    elif "custom" in modes:
        approval_mode = "custom"
    else:
        approval_mode = "standard"
    return ToolPermissionConfig(
        approval_mode=approval_mode,
        category_defaults=category_defaults,
        tools=tools,
    )


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


def _conversation_tool_permissions(conversation: Conversation) -> ToolPermissionConfig | None:
    settings = getattr(conversation, "settings", {}) or {}
    raw_permissions = settings.get("tool_permissions") if isinstance(settings, Mapping) else None
    if not isinstance(raw_permissions, Mapping):
        return None
    try:
        return ToolPermissionConfig.from_dict(raw_permissions)
    except Exception as exc:
        logger.debug("Failed to load conversation tool permissions: %s", exc)
        return None


class RunPolicyBuilder:
    """Build the single RunPolicy used by every runtime entry point."""

    @staticmethod
    def build(
        *,
        conversation: Conversation,
        app_settings: Mapping | None = None,
        mode_slug: str | None = None,
        reasoning_enabled: bool | None = None,
        reasoning_effort: str | None = None,
        show_thinking: bool | None = None,
        work_dir: str | None = None,
        mode_manager: ModeManager | None = None,
        retry_config: RetryConfig | None = None,
        tool_selection: ToolSelectionPolicy | None = None,
        tool_permissions: ToolPermissionConfig | None = None,
        disabled_tools: Iterable[str] = (),
        source: str = "desktop",
    ) -> RunPolicy:
        app_settings_dict = _settings_dict(app_settings)
        settings = getattr(conversation, "settings", {}) or {}
        settings = settings if isinstance(settings, Mapping) else {}

        slug = str(mode_slug or getattr(conversation, "mode", "") or "chat").strip() or "chat"

        llm_config = LLMConfig.from_conversation(conversation)
        if reasoning_enabled is None:
            reasoning_enabled = llm_config.reasoning_enabled
        if reasoning_effort is None:
            reasoning_effort = llm_config.reasoning_effort
        reasoning_effort = str(reasoning_effort or "").strip().lower()
        if reasoning_enabled is False and reasoning_effort:
            raise ValueError("reasoning_effort must be empty when reasoning is disabled")
        if show_thinking is None:
            show_thinking_default = bool(app_settings_dict.get("show_thinking", True))
            show_thinking = bool(settings.get("show_thinking", show_thinking_default))

        retry = retry_config or _load_retry_config(app_settings_dict)
        selection = ToolSelectionPolicy.all()
        conversation_selection = _conversation_tool_selection(conversation)
        if conversation_selection is not None:
            selection = selection.intersect(conversation_selection)
        if tool_selection is not None:
            selection = selection.intersect(tool_selection)

        base_permissions = tool_permissions or _load_tool_permissions(app_settings_dict)
        conversation_permissions = _conversation_tool_permissions(conversation)
        effective_permissions = _merge_tool_permissions(base_permissions, conversation_permissions)
        disabled = [str(name or "").strip() for name in disabled_tools if str(name or "").strip()]
        if disabled:
            effective_permissions = ToolPermissionConfig(
                approval_mode=effective_permissions.approval_mode,
                category_defaults=dict(effective_permissions.category_defaults or {}),
                tools={
                    **dict(effective_permissions.tools or {}),
                    **{name: ToolPolicy(enabled=False, auto_approve=False) for name in disabled},
                },
            )

        manager = mode_manager or ModeManager(work_dir or getattr(conversation, "work_dir", None) or None)
        policy = build_run_policy(
            mode_slug=slug,
            reasoning_enabled=reasoning_enabled,
            reasoning_effort=reasoning_effort,
            show_thinking=bool(show_thinking),
            tool_selection=selection,
            mode_manager=manager,
            retry_config=retry,
            tool_permissions=effective_permissions,
        )
        agent_config = _load_agent_runtime_config(app_settings_dict)
        try:
            mode_cfg = manager.get(slug)
        except Exception:
            mode_cfg = None

        updates: dict[str, object] = {"source": str(source or "desktop")}
        mode_has_turn_budget = False
        if mode_cfg is not None:
            mode_has_turn_budget = bool(getattr(mode_cfg, "max_turns", None))
        if not mode_has_turn_budget:
            updates["max_turns"] = int(agent_config.max_turns or policy.max_turns)

        return replace(
            policy,
            **updates,
        )
