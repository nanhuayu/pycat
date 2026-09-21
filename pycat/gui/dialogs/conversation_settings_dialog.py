"""Conversation settings dialog (per-conversation overrides)."""

from __future__ import annotations

import logging
from typing import List, Optional

from PyQt6.QtCore import QSize, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pycat.core.app.state import ConversationSettingsUpdate
from pycat.core.config import AppConfig, load_app_config
from pycat.core.modes.manager import ModeManager
from pycat.gui.utils.combo_box import configure_combo_popup
from pycat.gui.utils.form_builder import FormSection
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.widgets.model_ref_selector import ModelRefCombo
from pycat.gui.widgets.tool_category_selector import ToolCategorySelector
from pycat.models.contracts.agent import effective_pycat_assistant_enabled
from pycat.models.contracts.tooling import TOOL_CATEGORIES, ToolSelectionPolicy
from pycat.models.conversation import Conversation
from pycat.models.model_ref import build_model_ref, normalize_provider_name, split_model_ref
from pycat.models.provider import Provider

logger = logging.getLogger(__name__)


class ConversationSettingsDialog(QDialog):
    model_edit_requested = pyqtSignal(str)

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
        self.setMinimumSize(620, 580)

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

        tabs = QTabWidget()
        tabs.setObjectName("conversation_settings_tabs")
        root.addWidget(tabs, 1)

        common_body = self._add_scroll_tab(tabs, "常用")
        context_body = self._add_scroll_tab(tabs, "上下文")
        tools_body = self._add_scroll_tab(tabs, "工具与频道")

        settings = conversation.settings or {}
        self._system_prompt_display_text = str(settings.get("session_instructions") or "").strip()
        self._pycat_assistant_enabled = effective_pycat_assistant_enabled(
            settings.get("pycat_assistant_enabled", True),
            mode=str(getattr(conversation, "mode", "chat") or "chat"),
        )

        self._build_basic_section(common_body, conversation)
        self._build_model_section(common_body, conversation)
        self._build_feature_section(common_body, settings)
        common_body.addStretch()

        self._build_system_prompt_section(context_body)
        context_body.addWidget(self._build_memory_policy_group(settings))
        context_body.addStretch()

        self._build_tool_selection_section(tools_body, settings)
        tools_body.addWidget(self._build_channel_policy_group(settings))
        tools_body.addStretch()

        self._on_mode_changed(self.mode_combo.currentIndex())

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.save_btn = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.save_btn.setText("保存")
        self.save_btn.setProperty("primary", True)
        self.cancel_btn.setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _add_scroll_tab(tabs: QTabWidget, title: str) -> QVBoxLayout:
        scroll = QScrollArea()
        scroll.setObjectName("conversation_settings_scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content.setObjectName("conversation_settings_content")
        body = QVBoxLayout(content)
        body.setContentsMargins(8, 8, 8, 8)
        body.setSpacing(10)
        scroll.setWidget(content)
        tabs.addTab(scroll, title)
        return body

    def _build_basic_section(self, body: QVBoxLayout, conversation: Conversation) -> None:
        section = FormSection("基本信息")
        self.title_edit = section.add_line_edit("名称", text=conversation.title or "", object_name="conv_title")

        self.mode_combo = QComboBox()
        self.mode_combo.setObjectName("conv_mode")
        configure_combo_popup(self.mode_combo)
        self.mode_combo.blockSignals(True)
        try:
            manager = ModeManager(getattr(conversation, "work_dir", "") or None, data_dir=getattr(conversation, "data_dir", None))
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
        body.addWidget(section.group)

    def _build_system_prompt_section(self, body: QVBoxLayout) -> None:
        section = FormSection("会话指令")
        self.pycat_assistant_check = section.add_checkbox(
            "使用 PyCat 助手提示",
            checked=bool(self._pycat_assistant_enabled),
            row_label="默认提示",
            object_name="conv_pycat_assistant",
        )
        self.pycat_assistant_check.setToolTip("注入 PyCat 默认提示和当前环境信息；仅 Chat 模式可关闭。")
        self.pycat_assistant_check.toggled.connect(self._on_pycat_assistant_toggled)
        self.system_prompt_edit = section.add_text_edit(
            "内容",
            text=self._system_prompt_display_text,
            placeholder="追加本会话的指令",
            max_height=140,
            object_name="conv_system_prompt",
        )
        self.system_prompt_note = QLabel("会话指令只会追加到当前请求，不替换全局或模式规则。")
        self.system_prompt_note.setWordWrap(True)
        self.system_prompt_note.setProperty("muted", True)
        self.system_prompt_edit.setPlaceholderText("仅追加到其它显式指令之后")
        section.form.addRow("", self.system_prompt_note)
        body.addWidget(section.group)

    def _build_model_section(self, body: QVBoxLayout, conversation: Conversation) -> None:
        section = FormSection("会话模型")

        self.primary_model_combo = ModelRefCombo(
            self._providers,
            current_model_ref=self._current_primary_model_ref(conversation),
            allow_empty=False,
            empty_label="选择主模型",
        )
        self.primary_model_combo.setObjectName("conv_primary_model")
        model_row = QWidget()
        model_row_layout = QHBoxLayout(model_row)
        model_row_layout.setContentsMargins(0, 0, 0, 0)
        model_row_layout.setSpacing(6)
        model_row_layout.addWidget(self.primary_model_combo, 1)
        self.edit_model_btn = QToolButton()
        self.edit_model_btn.setObjectName("toolbar_btn")
        self.edit_model_btn.setIcon(Icons.get(Icons.EDIT))
        self.edit_model_btn.setIconSize(QSize(18, 18))
        self.edit_model_btn.setFixedSize(30, 30)
        self.edit_model_btn.setToolTip("编辑模型")
        self.edit_model_btn.setAccessibleName("编辑模型")
        self.edit_model_btn.clicked.connect(
            lambda: self.model_edit_requested.emit(self.primary_model_combo.model_ref().strip())
        )
        model_row_layout.addWidget(self.edit_model_btn)
        section.form.addRow("主模型", model_row)

        self.edit_model_btn.setEnabled(bool(self.primary_model_combo.model_ref().strip()))
        self.primary_model_combo.currentIndexChanged.connect(self._on_primary_model_changed)

        llm_config = self._conversation.get_llm_config()
        self.stream_enabled = section.add_checkbox(
            "启用",
            checked=llm_config.resolved_stream(default=True),
            row_label="流式输出",
            object_name="conv_stream",
        )

        self.max_tokens_spin = QSpinBox()
        self.max_tokens_spin.setRange(0, 10_000_000)
        self.max_tokens_spin.setSingleStep(1024)
        self.max_tokens_spin.setSpecialValueText("继承模型")
        self.max_tokens_spin.setValue(int(llm_config.max_tokens or 0))
        self.max_tokens_spin.setToolTip(
            "本次请求输出上限；0 表示使用产品默认 65,536，再按模型档案能力上限和总窗口校准。"
        )
        section.form.addRow("本次输出上限", self.max_tokens_spin)

        hint = QLabel("推理强度和协议统一在“编辑模型”中设置。")
        hint.setProperty("muted", True)
        section.form.addRow("推理", hint)

        body.addWidget(section.group)

    def _build_tool_selection_section(self, body: QVBoxLayout, settings: dict) -> None:
        group = QGroupBox("模型工具调用能力")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        self.tool_category_selector = ToolCategorySelector(
            columns=2,
            object_prefix="conv_tool_category",
            allow_inherit=True,
            show_ids=True,
        )
        self.tool_category_checks = self.tool_category_selector.checks
        layout.addWidget(self.tool_category_selector)
        body.addWidget(group)

    def _build_feature_section(self, body: QVBoxLayout, settings: dict) -> None:
        section = FormSection("显示")
        show_thinking = settings.get("show_thinking")
        self.show_thinking = section.add_checkbox(
            "显示推理过程",
            checked=show_thinking if isinstance(show_thinking, bool) else self._default_show_thinking,
            object_name="conv_show_thinking",
        )
        body.addWidget(section.group)

    def _on_primary_model_changed(self, _index: int) -> None:
        self.edit_model_btn.setEnabled(bool(self.primary_model_combo.model_ref().strip()))

    def _selected_model_profile(self):
        provider = self._resolve_provider_from_model_ref(self.primary_model_combo.model_ref().strip())
        _provider_name, model_id = split_model_ref(self.primary_model_combo.model_ref().strip())
        return provider.effective_model_profile(model_id) if provider is not None and model_id else None

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
        return ConversationSettingsUpdate(
            title=(self.title_edit.text() or "").strip(),
            provider_id=str(provider_id or "").strip(),
            provider_name=str(provider_name or "").strip(),
            api_type=str(api_type or "").strip().lower(),
            model=str(model or "").strip(),
            primary_model_ref=normalized_primary_ref,
            mode_slug=str(mode_slug or "chat").strip() or "chat",
            session_instructions=(self.system_prompt_edit.toPlainText() or "").strip(),
            pycat_assistant_enabled=(
                bool(self._pycat_assistant_enabled)
                if str(mode_slug or "chat").strip().lower() == "chat"
                else True
            ),
            stream=bool(self.stream_enabled.isChecked()),
            max_tokens=self.max_tokens_spin.value() or None,
            show_thinking=bool(self.show_thinking.isChecked()),
            memory_enabled=bool(self.memory_enabled_check.isChecked()),
            tool_selection=self._selected_tool_selection(),
            allowed_channel_sources=self._selected_allowed_channel_sources(),
            trusted_channel_sources=self._selected_trusted_channel_sources(),
            channel_notice_policy=str(self.channel_notice_combo.currentData() or "notice").strip() or "notice",
        )

    def _build_memory_policy_group(self, settings: dict) -> QGroupBox:
        group = QGroupBox("记忆策略")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        memory_enabled = settings.get("memory_enabled")
        self.memory_enabled_check = QCheckBox("记住有用的偏好与项目经验")
        self.memory_enabled_check.setChecked(memory_enabled if isinstance(memory_enabled, bool) else True)
        layout.addWidget(self.memory_enabled_check)

        hint = QLabel(
            "任务结束后在后台整理，下次运行时使用。关闭后停止收集、整理和使用记忆；已有内容仍可在右侧“记忆”中查看，重新启用后可编辑或忘记。"
        )
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
        if slug == "chat":
            self.pycat_assistant_check.setEnabled(True)
            self.pycat_assistant_check.setChecked(bool(self._pycat_assistant_enabled))
            self.pycat_assistant_check.setToolTip("注入 PyCat 默认提示和当前环境信息；可关闭用于原生模型提示词调试。")
        else:
            self.pycat_assistant_check.setEnabled(False)
            self.pycat_assistant_check.setChecked(True)
            self.pycat_assistant_check.setToolTip("Agent、Plan、Review 和频道模式固定使用运行规则。")
        try:
            manager = ModeManager(getattr(self._conversation, "work_dir", "") or None)
            mode = manager.get(slug)
            tool_categories = set(mode.tool_category_names())
        except Exception:
            tool_categories = set()

        self._refresh_tool_selection_checks(tool_categories)

    def _on_pycat_assistant_toggled(self, checked: bool) -> None:
        if self.pycat_assistant_check.isEnabled():
            self._pycat_assistant_enabled = bool(checked)

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
        selector = getattr(self, "tool_category_selector", None)
        if selector is None:
            return
        mode_allowed = set(mode_categories if mode_categories is not None else self._current_mode_tool_categories())
        ceiling, path = self._tool_selection_parent(mode_allowed)
        settings_selection = self._settings_tool_selection()
        selector.set_policy(ceiling=ceiling, policy=settings_selection, inheritance_path=path)

    def _tool_selection_parent(self, mode_categories: set[str]) -> tuple[set[str], str]:
        ceiling = set(mode_categories)
        path = "继承路径：Mode"
        settings = self._conversation.settings or {}
        binding = settings.get("channel_binding") if isinstance(settings.get("channel_binding"), dict) else {}
        channel_id = str(binding.get("channel_id") or "").strip()
        channel = next(
            (
                item for item in getattr(self._app_config, "channels", ()) or ()
                if str(getattr(item, "id", "") or "").strip() == channel_id
            ),
            None,
        )
        channel_selection = getattr(channel, "tool_selection", None) if channel is not None else None
        if channel_selection is not None and channel_selection.allowed_categories is not None:
            ceiling &= set(channel_selection.allowed_categories)
            path += " ∩ 频道实例"
        path += " ∩ 当前会话"
        return ceiling, path

    def _selected_tool_selection(self) -> ToolSelectionPolicy | None:
        return self.tool_category_selector.selection_policy()

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
