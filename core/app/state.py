from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from models.contracts.tooling import PermissionPreset, ToolSelectionPolicy
from models.contracts.mcp import McpServerConfig
from models.contracts.mode import ModeConfig
from models.provider import Provider
from models.search_config import SearchConfig


@dataclass(frozen=True)
class ConversationSelection:
    provider_id: str = ""
    provider_name: str = ""
    api_type: str = ""
    model: str = ""
    primary_model_ref: str = ""
    mode_slug: str = "chat"
    work_dir: str = ""
    show_thinking: bool = True
    permission_preset: PermissionPreset | None = None


@dataclass(frozen=True)
class ConversationSettingsUpdate:
    title: str = ""
    provider_id: str = ""
    provider_name: str = ""
    api_type: str = ""
    model: str = ""
    primary_model_ref: str = ""
    mode_slug: str = "chat"
    session_instructions: str = ""
    pycat_assistant_enabled: bool | None = None
    max_context_messages: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stream: bool | None = None
    show_thinking: bool = True
    memory_sources: tuple[str, ...] = ("session", "workspace", "global")
    tool_selection: ToolSelectionPolicy | None = None
    allowed_channel_sources: tuple[str, ...] = field(default_factory=tuple)
    trusted_channel_sources: tuple[str, ...] = field(default_factory=tuple)
    channel_notice_policy: str = "notice"


@dataclass(frozen=True)
class AppSettingsUpdate:
    providers: tuple[Provider, ...] = field(default_factory=tuple)
    settings_patch: dict[str, Any] = field(default_factory=dict)
    mcp_servers: tuple[McpServerConfig, ...] = field(default_factory=tuple)
    modes: tuple[ModeConfig, ...] = field(default_factory=tuple)
    search_config: SearchConfig | None = None


@dataclass(frozen=True)
class AppState:
    current_conversation_id: str = ""
    provider_id: str = ""
    provider_name: str = ""
    api_type: str = ""
    model: str = ""
    model_ref: str = ""
    mode_slug: str = "chat"
    work_dir: str = ""
    show_thinking: bool = True
    message_count: int = 0
    is_streaming: bool = False
    selected_memory_sources: tuple[str, ...] = ("session", "workspace", "global")
    enabled_channel_sources: tuple[str, ...] = field(default_factory=tuple)
    allowed_channel_sources: tuple[str, ...] = field(default_factory=tuple)
    trusted_channel_sources: tuple[str, ...] = field(default_factory=tuple)
    channel_notice_policy: str = "notice"
    available_provider_ids: tuple[str, ...] = field(default_factory=tuple)
    conversation_count: int = 0


EMPTY_APP_STATE = AppState()
