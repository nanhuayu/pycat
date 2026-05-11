"""Conversation settings dialog (per-conversation overrides)."""

from __future__ import annotations

import logging
from typing import List, Optional

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.app.state import ConversationSettingsUpdate
from core.config import AppConfig, load_app_config
from core.modes.manager import ModeManager
from core.prompts.system_builder import resolve_base_system_prompt_text
from core.tools.catalog import TOOL_CATEGORIES, TOOL_CATEGORY_LABELS, ToolSelectionPolicy
from models.conversation import Conversation
from models.provider import Provider, build_model_ref, normalize_provider_name, split_model_ref
from ui.utils.combo_box import configure_combo_popup
from ui.utils.form_builder import FormSection
from ui.widgets.model_ref_selector import ModelRefCombo


logger = logging.getLogger(__name__)


class ConversationSettingsDialog(QDialog):
    def __init__(
        self,
        conversation: Conversation,
        providers: Optional[List[Provider]] = None,
        default_show_thinking: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("对话设置")
        self.setObjectName("conversation_settings_dialog")
        self.setModal(True)
        self.setMinimumSize(500, 620)

        self._conversation = conversation
        self._providers = providers or []
        self._default_show_thinking = bool(default_show_thinking)
        try:
            self._app_config = load_app_config()
        except Exception:
            self._app_config = AppConfig()

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        scroll = QScrollArea()
        scroll.setObjectName("conversation_settings_scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        root.addWidget(scroll, 1)

        content = QWidget()
        content.setObjectName("conversation_settings_content")
        scroll.setWidget(content)

        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(10)

        settings = conversation.settings or {}
        self._system_prompt_base_text = self._compute_base_system_prompt()
        self._system_prompt_display_text = (settings.get("system_prompt", "") or "").strip() or self._system_prompt_base_text

        self._build_basic_section(body, conversation)
        self._build_model_section(body, conversation)
        self._build_tool_selection_section(body, settings)
        self._build_sampling_section(body, settings)
        self._build_feature_section(body, settings)
        body.addWidget(self._build_memory_policy_group(settings))
        body.addWidget(self._build_channel_policy_group(settings))
        body.addStretch()

        self._on_mode_changed(self.mode_combo.currentIndex())

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.cancel_btn)

        self.save_btn = QPushButton("保存")
        self.save_btn.setObjectName("primary_btn")
        self.save_btn.setProperty("primary", True)
        self.save_btn.clicked.connect(self.accept)
        btn_row.addWidget(self.save_btn)

        root.addLayout(btn_row)

    def _build_basic_section(self, body: QVBoxLayout, conversation: Conversation) -> None:
        section = FormSection("基本信息")
        self.title_edit = section.add_line_edit("名称", text=conversation.title or "", object_name="conv_title")
        self.system_prompt_edit = section.add_text_edit(
            "系统提示",
            text=self._system_prompt_display_text,
            placeholder="显示当前生效的基础 system prompt，可直接修改",
            max_height=140,
            object_name="conv_system_prompt",
        )
        self.system_prompt_note = QLabel("当前显示的是该模式下生效的基础 system prompt。保持不改时不会额外保存对话级覆盖。")
        self.system_prompt_note.setWordWrap(True)
        self.system_prompt_note.setProperty("muted", True)
        section.form.addRow("", self.system_prompt_note)
        body.addWidget(section.group)

    def _build_model_section(self, body: QVBoxLayout, conversation: Conversation) -> None:
        section = FormSection("会话模型")
        settings = conversation.settings or {}

        self.primary_model_combo = ModelRefCombo(
            self._providers,
            current_model_ref=self._current_primary_model_ref(conversation),
            allow_empty=False,
            empty_label="选择主模型",
        )
        self.primary_model_combo.setObjectName("conv_primary_model")
        section.form.addRow("主模型", self.primary_model_combo)

        self.secondary_model_combo = ModelRefCombo(
            self._providers,
            current_model_ref=str(settings.get("secondary_model_ref") or ""),
            allow_empty=True,
            empty_label="不设置副模型",
        )
        self.secondary_model_combo.setObjectName("conv_secondary_model")
        section.form.addRow("副模型", self.secondary_model_combo)

        self.fallback_model_combo = ModelRefCombo(
            self._providers,
            current_model_ref=str(settings.get("fallback_model_ref") or ""),
            allow_empty=True,
            empty_label="不设置备用模型",
        )
        self.fallback_model_combo.setObjectName("conv_fallback_model")
        section.form.addRow("备用模型", self.fallback_model_combo)

        self.mode_combo = QComboBox()
        self.mode_combo.setObjectName("conv_mode")
        configure_combo_popup(self.mode_combo)
        self.mode_combo.blockSignals(True)
        try:
            manager = ModeManager(getattr(conversation, "work_dir", "") or None)
            for mode in manager.list_ui_modes():
                self.mode_combo.addItem(mode.name, mode.slug)
            current_slug = str(getattr(conversation, "mode", "chat") or "chat")
            if self.mode_combo.findData(current_slug) < 0:
                current_mode = manager.get(current_slug)
                if current_mode.slug == current_slug:
                    self.mode_combo.addItem(current_mode.name, current_mode.slug)
        except Exception:
            self.mode_combo.addItem("Chat", "chat")
            self.mode_combo.addItem("Agent", "agent")
        try:
            current_slug = str(getattr(conversation, "mode", "chat") or "chat")
            index = self.mode_combo.findData(current_slug)
            if index >= 0:
                self.mode_combo.setCurrentIndex(index)
        except Exception as exc:
            logger.debug("Failed to restore conversation mode selection in settings dialog: %s", exc)
        self.mode_combo.blockSignals(False)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        section.form.addRow("模式", self.mode_combo)

        self.provider_hint = QLabel("主/副/备用是当前对话的运行角色；模型设置页只维护服务商连接与模型能力。")
        self.provider_hint.setWordWrap(True)
        self.provider_hint.setProperty("muted", True)
        section.form.addRow("", self.provider_hint)
        body.addWidget(section.group)

    def _build_tool_selection_section(self, body: QVBoxLayout, settings: dict) -> None:
        group = QGroupBox("模型工具调用能力")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        self.tool_category_checks: dict[str, QCheckBox] = {}
        for category in TOOL_CATEGORIES:
            check = QCheckBox(f"{TOOL_CATEGORY_LABELS.get(category, category)} ({category})")
            check.setObjectName(f"conv_tool_category_{category}")
            check.setToolTip("当前会话中模型可见的工具类别；具体工具启用和自动批准仍由 设置 → 权限 控制。")
            layout.addWidget(check)
            self.tool_category_checks[category] = check

        hint = QLabel("这里控制当前会话里模型能看见哪些工具类别；可在模式默认类别外增减，但具体工具启用、禁用和自动批准仍由 设置 → 权限 控制。")
        hint.setWordWrap(True)
        hint.setProperty("muted", True)
        layout.addWidget(hint)
        body.addWidget(group)

    def _build_sampling_section(self, body: QVBoxLayout, settings: dict) -> None:
        group = QGroupBox("采样与上下文")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        hint = QLabel(
            "温度、Top P 和最大输出 Token 属于服务商-模型能力/默认值；"
            "上下文消息数使用全局上下文策略。当前对话只保留模型角色、工具、记忆和通道等会话级设置。"
        )
        hint.setWordWrap(True)
        hint.setProperty("muted", True)
        layout.addWidget(hint)
        body.addWidget(group)

    def _build_feature_section(self, body: QVBoxLayout, settings: dict) -> None:
        group = QGroupBox("功能开关")
        layout = QHBoxLayout(group)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(16)

        self.stream_enabled = QCheckBox("流式输出")
        self.stream_enabled.setObjectName("conv_stream")
        self.stream_enabled.setChecked(bool(settings.get("stream", True)))
        layout.addWidget(self.stream_enabled)

        self.show_thinking = QCheckBox("显示思考")
        self.show_thinking.setObjectName("conv_show_thinking")
        show_thinking = settings.get("show_thinking")
        self.show_thinking.setChecked(show_thinking if isinstance(show_thinking, bool) else self._default_show_thinking)
        layout.addWidget(self.show_thinking)

        layout.addStretch()
        body.addWidget(group)

    def build_update(self) -> ConversationSettingsUpdate:
        primary_model_ref = self.primary_model_combo.model_ref().strip()
        provider = self._resolve_provider_from_model_ref(primary_model_ref)
        provider_token, model = split_model_ref(primary_model_ref)
        if provider is not None:
            provider_id = str(getattr(provider, "id", "") or "").strip()
            provider_name = str(getattr(provider, "name", "") or "").strip()
            api_type = str(getattr(provider, "api_type", "") or "").strip().lower()
        else:
            provider_id = ""
            provider_name = provider_token
            api_type = ""
            if not provider_name:
                existing = self._resolve_provider_by_id(str(getattr(self._conversation, "provider_id", "") or ""))
                if existing is not None:
                    provider_id = str(getattr(existing, "id", "") or "").strip()
                    provider_name = str(getattr(existing, "name", "") or "").strip()
                    api_type = str(getattr(existing, "api_type", "") or "").strip().lower()

        normalized_primary_ref = build_model_ref(provider_name, model) if provider_name and model else primary_model_ref
        mode_slug = self.mode_combo.currentData() if hasattr(self, "mode_combo") else "chat"
        system_prompt_text = (self.system_prompt_edit.toPlainText() or "").strip()
        return ConversationSettingsUpdate(
            title=(self.title_edit.text() or "").strip(),
            provider_id=str(provider_id or "").strip(),
            provider_name=str(provider_name or "").strip(),
            api_type=str(api_type or "").strip().lower(),
            model=str(model or "").strip(),
            primary_model_ref=normalized_primary_ref,
            secondary_model_ref=self.secondary_model_combo.model_ref().strip(),
            fallback_model_ref=self.fallback_model_combo.model_ref().strip(),
            mode_slug=str(mode_slug or "chat").strip() or "chat",
            system_prompt=system_prompt_text if system_prompt_text and system_prompt_text != self._system_prompt_base_text else "",
            stream=bool(self.stream_enabled.isChecked()),
            show_thinking=bool(self.show_thinking.isChecked()),
            memory_sources=self._selected_memory_sources(),
            tool_selection=self._selected_tool_selection(),
            allowed_channel_sources=self._selected_allowed_channel_sources(),
            trusted_channel_sources=self._selected_trusted_channel_sources(),
            channel_notice_policy=str(self.channel_notice_combo.currentData() or "notice").strip() or "notice",
        )

    def _build_memory_policy_group(self, settings: dict) -> QGroupBox:
        selected_sources = self._resolve_memory_sources_from_settings(settings)

        group = QGroupBox("记忆策略")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        self.memory_session_check = QCheckBox("使用会话记忆（facts；todo/artifact 独立管理）")
        self.memory_session_check.setChecked("session" in selected_sources)
        layout.addWidget(self.memory_session_check)

        self.memory_workspace_check = QCheckBox("使用工作区记忆（.pycat/memory / MEMORY.md）")
        self.memory_workspace_check.setChecked("workspace" in selected_sources)
        layout.addWidget(self.memory_workspace_check)

        self.memory_global_check = QCheckBox("使用全局记忆（~/.PyCat/memory / SOUL.md）")
        self.memory_global_check.setChecked("global" in selected_sources)
        layout.addWidget(self.memory_global_check)

        hint = QLabel("记忆来源按会话单独控制：关闭后不会删除已有记忆，只是不再注入到当前对话上下文。")
        hint.setWordWrap(True)
        hint.setProperty("muted", True)
        layout.addWidget(hint)
        return group

    def _build_channel_policy_group(self, settings: dict) -> QGroupBox:
        group = QGroupBox("频道策略")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        self.channel_notice_combo = QComboBox()
        configure_combo_popup(self.channel_notice_combo)
        self.channel_notice_combo.addItem("默认提醒来源", "notice")
        self.channel_notice_combo.addItem("严格限制未信任来源", "strict")
        self.channel_notice_combo.addItem("简洁提示来源", "silent")

        notice_row = QHBoxLayout()
        notice_row.setSpacing(8)
        notice_row.addWidget(QLabel("来源提示策略"))
        notice_row.addWidget(self.channel_notice_combo, 1)
        layout.addLayout(notice_row)

        self._channel_allow_checks: dict[str, QCheckBox] = {}
        self._channel_trust_checks: dict[str, QCheckBox] = {}

        enabled_channels = self._enabled_channel_configs()
        allowed_sources = self._resolve_allowed_channel_sources_from_settings(settings)
        trusted_sources = self._resolve_trusted_channel_sources_from_settings(settings, allowed_sources)

        current_notice = str(settings.get("channel_notice_policy", "notice") or "notice").strip().lower() or "notice"
        idx = self.channel_notice_combo.findData(current_notice)
        self.channel_notice_combo.setCurrentIndex(idx if idx >= 0 else 0)

        if not enabled_channels:
            empty = QLabel("当前没有启用的外部频道来源。可先到设置页的“频道”中配置来源，再在这里做会话级允许/信任控制。")
            empty.setWordWrap(True)
            empty.setProperty("muted", True)
            layout.addWidget(empty)
            return group

        for channel in enabled_channels:
            source = str(getattr(channel, "source", "") or "").strip()
            if not source:
                continue

            row = QHBoxLayout()
            row.setSpacing(8)

            allow_check = QCheckBox(self._channel_label(channel))
            allow_check.setChecked(source in allowed_sources)
            allow_check.setToolTip(source)
            row.addWidget(allow_check, 1)

            trust_check = QCheckBox("可信来源")
            trust_check.setChecked(source in trusted_sources and source in allowed_sources)
            trust_check.setEnabled(bool(allow_check.isChecked()))
            trust_check.setToolTip("可信来源会在 prompt 中作为较高置信度的运行上下文，但仍不会覆盖系统规则。")
            row.addWidget(trust_check)

            allow_check.toggled.connect(lambda checked, src=source: self._on_channel_allow_toggled(src, checked))
            layout.addLayout(row)

            source_label = QLabel(f"来源标识：{source}")
            source_label.setProperty("muted", True)
            source_label.setWordWrap(True)
            layout.addWidget(source_label)

            self._channel_allow_checks[source] = allow_check
            self._channel_trust_checks[source] = trust_check

        hint = QLabel("允许来源控制哪些外部频道可进入当前会话；可信来源是允许来源的子集，用于更清晰地表达 trust boundary。")
        hint.setWordWrap(True)
        hint.setProperty("muted", True)
        layout.addWidget(hint)
        return group

    def _on_mode_changed(self, index: int) -> None:
        if not hasattr(self, "show_thinking"):
            return
        slug = str(self.mode_combo.itemData(index) or "chat").strip().lower()
        settings = self._conversation.settings or {}
        try:
            manager = ModeManager(getattr(self._conversation, "work_dir", "") or None)
            mode = manager.get(slug)
            tool_categories = set(mode.tool_category_names())
        except Exception:
            tool_categories = set()

        previous_base = self._system_prompt_base_text
        self._system_prompt_base_text = self._compute_base_system_prompt(slug)
        current_text = (self.system_prompt_edit.toPlainText() or "").strip()
        if not current_text or current_text == previous_base:
            self.system_prompt_edit.blockSignals(True)
            try:
                self.system_prompt_edit.setPlainText(self._system_prompt_base_text)
            finally:
                self.system_prompt_edit.blockSignals(False)

        default_thinking = bool({"execute", "edit"} & tool_categories)

        if "show_thinking" not in settings:
            self.show_thinking.setChecked(default_thinking if slug != "chat" else self._default_show_thinking)

        self._refresh_tool_selection_checks(tool_categories)

    def _current_mode_tool_categories(self) -> set[str]:
        slug = str(self.mode_combo.currentData() or "chat").strip().lower() if hasattr(self, "mode_combo") else "chat"
        try:
            manager = ModeManager(getattr(self._conversation, "work_dir", "") or None)
            return set(manager.get(slug).tool_category_names())
        except Exception:
            return set(TOOL_CATEGORIES)

    def _settings_tool_selection(self) -> ToolSelectionPolicy | None:
        settings = self._conversation.settings or {}
        raw = settings.get("tool_selection")
        if not isinstance(raw, dict):
            return None
        try:
            return ToolSelectionPolicy.from_dict(raw)
        except Exception as exc:
            logger.debug("Failed to load conversation tool selection: %s", exc)
            return None

    def _refresh_tool_selection_checks(self, mode_categories: set[str] | None = None) -> None:
        checks = getattr(self, "tool_category_checks", None)
        if not checks:
            return
        mode_allowed = set(mode_categories if mode_categories is not None else self._current_mode_tool_categories())
        settings_selection = self._settings_tool_selection()
        if settings_selection is None or settings_selection.allowed_categories is None:
            selected = set(mode_allowed)
        else:
            selected = set(settings_selection.allowed_categories)

        for category, check in checks.items():
            check.blockSignals(True)
            try:
                check.setEnabled(True)
                check.setChecked(category in selected)
                if category in mode_allowed:
                    check.setToolTip("当前模式默认包含该类别；可在当前会话中取消。具体工具启用和自动批准仍由 设置 → 权限 控制。")
                else:
                    check.setToolTip("当前模式默认不包含该类别；可在当前会话中额外启用。具体工具启用和自动批准仍由 设置 → 权限 控制。")
            finally:
                check.blockSignals(False)

    def _selected_tool_selection(self) -> ToolSelectionPolicy:
        categories = [
            category
            for category, check in getattr(self, "tool_category_checks", {}).items()
            if check.isChecked()
        ]
        return ToolSelectionPolicy.from_categories(categories)

    def _current_primary_model_ref(self, conversation: Conversation) -> str:
        settings = conversation.settings or {}
        explicit = str(settings.get("primary_model_ref") or "").strip()
        if explicit:
            return explicit
        llm_config = conversation.get_llm_config()
        provider_name = self._resolve_provider_name(
            str(llm_config.provider_id or getattr(conversation, "provider_id", "") or "").strip()
        ) or str(llm_config.provider_name or getattr(conversation, "provider_name", "") or "").strip()
        model = str(llm_config.model or getattr(conversation, "model", "") or "").strip()
        return build_model_ref(provider_name, model)

    def _resolve_provider_by_id(self, provider_id: str) -> Provider | None:
        normalized_id = str(provider_id or "").strip()
        if not normalized_id:
            return None
        for provider in self._providers:
            if getattr(provider, "id", "") == normalized_id:
                return provider
        return None

    def _resolve_provider_name(self, provider_id: str) -> str:
        provider = self._resolve_provider_by_id(provider_id)
        if provider is not None:
            return str(getattr(provider, "name", "") or "").strip()
        return ""

    def _resolve_provider_api_type(self, provider_id: str) -> str:
        normalized_id = str(provider_id or "").strip()
        if not normalized_id:
            return ""
        for provider in self._providers:
            if getattr(provider, "id", "") == normalized_id:
                return str(getattr(provider, "api_type", "") or "").strip().lower()
        return ""

    def _resolve_provider_from_model_ref(self, model_ref: str) -> Provider | None:
        provider_token, _model = split_model_ref(model_ref)
        if not provider_token:
            return None
        normalized = normalize_provider_name(provider_token)
        for provider in self._providers:
            if normalize_provider_name(getattr(provider, "name", "")) == normalized:
                return provider
        return None

    def _enabled_channel_configs(self) -> list:
        channels = []
        for channel in getattr(self._app_config, "channels", []) or []:
            if not bool(getattr(channel, "enabled", False)):
                continue
            source = str(getattr(channel, "source", "") or "").strip()
            if not source:
                continue
            channels.append(channel)
        return channels

    @staticmethod
    def _normalize_sources(raw: object, *, allowed: tuple[str, ...] | None = None) -> tuple[str, ...]:
        if isinstance(raw, str):
            candidates = [part.strip() for part in raw.split(",")]
        elif isinstance(raw, (list, tuple, set)):
            candidates = [str(item).strip() for item in raw]
        else:
            candidates = []

        allowed_set = {item for item in (allowed or ()) if item}
        seen: set[str] = set()
        normalized: list[str] = []
        for item in candidates:
            if not item or item in seen:
                continue
            if allowed_set and item not in allowed_set:
                continue
            seen.add(item)
            normalized.append(item)
        return tuple(normalized)

    def _resolve_memory_sources_from_settings(self, settings: dict) -> tuple[str, ...]:
        normalized = self._normalize_sources(settings.get("memory_sources"), allowed=("session", "workspace", "global"))
        return normalized or ("session", "workspace", "global")

    def _resolve_allowed_channel_sources_from_settings(self, settings: dict) -> tuple[str, ...]:
        enabled_sources = tuple(
            str(getattr(channel, "source", "") or "").strip()
            for channel in self._enabled_channel_configs()
            if str(getattr(channel, "source", "") or "").strip()
        )
        normalized = self._normalize_sources(settings.get("allowed_channel_sources"), allowed=enabled_sources)
        return normalized or enabled_sources

    def _resolve_trusted_channel_sources_from_settings(self, settings: dict, allowed_sources: tuple[str, ...]) -> tuple[str, ...]:
        return self._normalize_sources(settings.get("trusted_channel_sources"), allowed=allowed_sources)

    def _selected_memory_sources(self) -> tuple[str, ...]:
        sources = []
        if getattr(self, "memory_session_check", None) and self.memory_session_check.isChecked():
            sources.append("session")
        if getattr(self, "memory_workspace_check", None) and self.memory_workspace_check.isChecked():
            sources.append("workspace")
        if getattr(self, "memory_global_check", None) and self.memory_global_check.isChecked():
            sources.append("global")
        return tuple(sources)

    def _selected_allowed_channel_sources(self) -> tuple[str, ...]:
        return tuple(
            source
            for source, checkbox in getattr(self, "_channel_allow_checks", {}).items()
            if checkbox.isChecked()
        )

    def _selected_trusted_channel_sources(self) -> tuple[str, ...]:
        allowed = set(self._selected_allowed_channel_sources())
        return tuple(
            source
            for source, checkbox in getattr(self, "_channel_trust_checks", {}).items()
            if checkbox.isChecked() and source in allowed
        )

    def _on_channel_allow_toggled(self, source: str, checked: bool) -> None:
        trust = getattr(self, "_channel_trust_checks", {}).get(source)
        if trust is None:
            return
        trust.setEnabled(bool(checked))
        if not checked:
            trust.setChecked(False)

    @staticmethod
    def _channel_label(channel) -> str:
        name = str(getattr(channel, "name", "") or "").strip()
        source = str(getattr(channel, "source", "") or "").strip()
        channel_type = str(getattr(channel, "type", "") or "channel").strip()
        return name or f"{channel_type} · {source.rsplit(':', 1)[-1] if source else channel_type}"

    def _compute_base_system_prompt(self, mode_slug: Optional[str] = None) -> str:
        conv = Conversation.from_dict(self._conversation.to_dict())
        if mode_slug:
            conv.mode = str(mode_slug or "chat") or "chat"
        return resolve_base_system_prompt_text(
            conversation=conv,
            app_config=self._app_config,
            default_work_dir=getattr(conv, "work_dir", ".") or ".",
            include_conversation_override=False,
        )
