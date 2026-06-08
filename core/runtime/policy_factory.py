from __future__ import annotations

import logging
from dataclasses import replace
from typing import Iterable, Mapping

from core.config.schema import AgentRuntimeConfig, RetryConfig, ToolPermissionConfig, ToolPolicy
from core.modes.manager import ModeManager
from core.task.builder import build_run_policy
from core.task.types import RunPolicy
from core.tools.catalog import ToolSelectionPolicy
from models.conversation import Conversation

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
    raw_permissions = _settings_dict(app_settings).get("permissions")
    if isinstance(raw_permissions, Mapping):
        try:
            return ToolPermissionConfig.from_dict(raw_permissions)
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
    category_defaults: dict[str, ToolPolicy] = {}
    tools: dict[str, ToolPolicy] = {}
    for config in configs:
        if config is None:
            continue
        category_defaults.update(dict(config.category_defaults or {}))
        tools.update(dict(config.tools or {}))
    return ToolPermissionConfig(category_defaults=category_defaults, tools=tools)


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


class RuntimePolicyFactory:
    """Build the single RunPolicy used by every runtime entry point."""

    @staticmethod
    def build(
        *,
        conversation: Conversation,
        app_settings: Mapping | None = None,
        mode_slug: str | None = None,
        enable_thinking: bool | None = None,
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

        if enable_thinking is None:
            show_thinking_default = bool(app_settings_dict.get("show_thinking", True))
            enable_thinking = bool(settings.get("show_thinking", show_thinking_default))

        retry = retry_config or _load_retry_config(app_settings_dict)
        selection = tool_selection or _conversation_tool_selection(conversation)

        base_permissions = tool_permissions or _load_tool_permissions(app_settings_dict)
        conversation_permissions = _conversation_tool_permissions(conversation)
        effective_permissions = _merge_tool_permissions(base_permissions, conversation_permissions)
        disabled = [str(name or "").strip() for name in disabled_tools if str(name or "").strip()]
        if disabled:
            effective_permissions = ToolPermissionConfig(
                category_defaults=dict(effective_permissions.category_defaults or {}),
                tools={
                    **dict(effective_permissions.tools or {}),
                    **{name: ToolPolicy(enabled=False, auto_approve=False) for name in disabled},
                },
            )

        manager = mode_manager or ModeManager(work_dir or getattr(conversation, "work_dir", None) or None)
        policy = build_run_policy(
            mode_slug=slug,
            enable_thinking=bool(enable_thinking),
            tool_selection=selection,
            mode_manager=manager,
            retry_config=retry,
            tool_permissions=effective_permissions,
        )
        agent_config = _load_agent_runtime_config(app_settings_dict)
        return replace(
            policy,
            source=str(source or "desktop"),
            force_agent_complete=bool(agent_config.force_agent_complete),
        )
