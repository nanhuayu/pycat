"""
Provider/service connection configuration dialog - Chinese UI
"""

import asyncio
import json
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from models.model_profile import ModelProfile
from models.provider import ANTHROPIC_NATIVE, OPENAI_COMPATIBLE, OPENAI_RESPONSES, OLLAMA_CHAT, Provider
from services.provider_service import ProviderService
from ui.utils.combo_box import configure_combo_popup
from ui.utils.icon_manager import Icons


class ProviderConfigDialog(QDialog):
    """Dialog for configuring an LLM provider/service connection."""

    models_updated = pyqtSignal(list)  # Emit when models are fetched

    def __init__(
        self,
        provider: Optional[Provider] = None,
        *,
        provider_service: Optional[ProviderService] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.provider = Provider.from_dict(provider.to_dict()) if provider is not None else Provider()
        self.provider_service = provider_service or ProviderService()
        self._loading_profile = False
        self._active_profile_model_id = ""
        self._setup_ui()
        self._load_provider()

    @staticmethod
    def _api_type_options() -> list[tuple[str, str]]:
        return [
            ("OpenAI 兼容 / Chat Completions", OPENAI_COMPATIBLE),
            ("OpenAI Responses API", OPENAI_RESPONSES),
            ("Anthropic 原生 / Messages API", ANTHROPIC_NATIVE),
            ("Ollama 本地 / Chat API", OLLAMA_CHAT),
        ]

    def _setup_ui(self):
        self.setWindowTitle("配置模型服务商")
        self.setObjectName("provider_config_dialog")
        self.setMinimumSize(600, 500)
        try:
            self.resize(660, 560)
        except Exception:
            pass

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        tabs = QTabWidget()

        # Basic tab
        basic_tab = QWidget()
        basic_layout = QVBoxLayout(basic_tab)
        basic_layout.setContentsMargins(8, 8, 8, 8)
        basic_layout.setSpacing(8)

        # Provider info
        info_group = QGroupBox("服务商连接")
        info_layout = QFormLayout(info_group)
        info_layout.setHorizontalSpacing(10)
        info_layout.setVerticalSpacing(6)

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("如: OpenAI, Claude, Ollama")
        info_layout.addRow("名称:", self.name_input)

        self.api_type_combo = QComboBox()
        configure_combo_popup(self.api_type_combo)
        for label, value in self._api_type_options():
            self.api_type_combo.addItem(label, value)
        self.api_type_combo.currentIndexChanged.connect(self._on_api_type_changed)
        info_layout.addRow("接口类型:", self.api_type_combo)

        self.api_type_help = QLabel("")
        self.api_type_help.setWordWrap(True)
        self.api_type_help.setProperty("muted", True)

        self.api_base_input = QLineEdit()
        self.api_base_input.setPlaceholderText("https://api.openai.com/v1")
        info_layout.addRow("API 地址:", self.api_base_input)

        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_input.setPlaceholderText("sk-...")

        key_layout = QHBoxLayout()
        key_layout.addWidget(self.api_key_input)
        show_key_btn = QPushButton()
        show_key_btn.setIcon(Icons.get(Icons.EYE, scale_factor=0.85))
        show_key_btn.setFixedWidth(40)
        show_key_btn.setFixedHeight(32)
        show_key_btn.setCheckable(True)

        def _toggle_key_visibility(checked: bool) -> None:
            self.api_key_input.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
            show_key_btn.setIcon(
                Icons.get(Icons.EYE_SLASH if checked else Icons.EYE, scale_factor=0.85)
            )

        show_key_btn.toggled.connect(_toggle_key_visibility)
        key_layout.addWidget(show_key_btn)
        info_layout.addRow("API Key:", key_layout)

        self.enabled_check = QCheckBox("启用该服务商")
        self.enabled_check.setChecked(True)
        info_layout.addRow("状态:", self.enabled_check)

        info_layout.addRow("说明:", self.api_type_help)

        basic_layout.addWidget(info_group)

        # Model settings
        model_group = QGroupBox("模型与能力")
        model_layout = QFormLayout(model_group)
        model_layout.setHorizontalSpacing(10)
        model_layout.setVerticalSpacing(6)

        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setMinimumWidth(220)
        configure_combo_popup(self.model_combo, popup_minimum_width=320)
        self.model_combo.currentTextChanged.connect(self._on_model_changed)

        fetch_btn = QPushButton()
        fetch_btn.setIcon(Icons.get(Icons.REFRESH, scale_factor=0.9))
        fetch_btn.setToolTip("获取可用模型")
        fetch_btn.setFixedWidth(38)
        fetch_btn.clicked.connect(self._fetch_models)

        model_picker = QWidget()
        model_picker_layout = QHBoxLayout(model_picker)
        model_picker_layout.setContentsMargins(0, 0, 0, 0)
        model_picker_layout.setSpacing(6)
        model_picker_layout.addWidget(self.model_combo, 1)
        model_picker_layout.addWidget(fetch_btn)
        model_layout.addRow("默认模型:", model_picker)

        self.models_label = QLabel("未加载模型")
        self.models_label.setWordWrap(True)
        self.models_label.setProperty("muted", True)
        model_layout.addRow("可用模型:", self.models_label)

        ability_widget = QWidget()
        ability_layout = QHBoxLayout(ability_widget)
        ability_layout.setContentsMargins(0, 0, 0, 0)
        ability_layout.setSpacing(12)
        self.model_vision_check = QCheckBox("视觉")
        self.model_tools_check = QCheckBox("工具调用")
        self.model_reasoning_check = QCheckBox("推理 / thinking")
        ability_layout.addWidget(self.model_vision_check)
        ability_layout.addWidget(self.model_tools_check)
        ability_layout.addWidget(self.model_reasoning_check)
        ability_layout.addStretch()
        model_layout.addRow("模型能力:", ability_widget)

        self.context_window_spin = QSpinBox()
        self.context_window_spin.setRange(0, 2_000_000)
        self.context_window_spin.setSingleStep(8192)
        self.context_window_spin.setSpecialValueText("不设置")
        self.context_window_spin.setMinimumWidth(112)

        self.max_output_spin = QSpinBox()
        self.max_output_spin.setRange(0, 200000)
        self.max_output_spin.setSingleStep(1024)
        self.max_output_spin.setSpecialValueText("不设置")
        self.max_output_spin.setMinimumWidth(112)

        self.temperature_spin = QDoubleSpinBox()
        self.temperature_spin.setRange(0.0, 2.0)
        self.temperature_spin.setDecimals(2)
        self.temperature_spin.setSingleStep(0.05)
        self.temperature_spin.setSpecialValueText("不设置")
        self.temperature_spin.setMinimumWidth(112)

        self.top_p_spin = QDoubleSpinBox()
        self.top_p_spin.setRange(0.0, 1.0)
        self.top_p_spin.setDecimals(2)
        self.top_p_spin.setSingleStep(0.05)
        self.top_p_spin.setSpecialValueText("不设置")
        self.top_p_spin.setMinimumWidth(112)

        self.reasoning_style_combo = QComboBox()
        configure_combo_popup(self.reasoning_style_combo)
        self.reasoning_style_combo.addItem("无", "none")
        self.reasoning_style_combo.addItem("OpenAI Responses reasoning", "openai_responses")
        self.reasoning_style_combo.addItem("OpenAI 兼容 reasoning_content", "openai_compatible")
        self.reasoning_style_combo.addItem("Anthropic thinking", "anthropic_thinking")
        self.reasoning_style_combo.addItem("通用 thinking", "thinking")

        self.reasoning_effort_combo = QComboBox()
        self.reasoning_effort_combo.setEditable(True)
        configure_combo_popup(self.reasoning_effort_combo)
        self.reasoning_effort_combo.addItem("不设置", "")
        self.reasoning_effort_combo.addItem("low", "low")
        self.reasoning_effort_combo.addItem("medium", "medium")
        self.reasoning_effort_combo.addItem("high", "high")
        self.reasoning_style_combo.setMinimumWidth(188)
        self.reasoning_effort_combo.setMinimumWidth(112)

        self.thinking_budget_spin = QSpinBox()
        self.thinking_budget_spin.setRange(0, 200000)
        self.thinking_budget_spin.setSingleStep(1024)
        self.thinking_budget_spin.setSpecialValueText("不设置")
        self.thinking_budget_spin.setMinimumWidth(112)

        def _right_label(text: str) -> QLabel:
            label = QLabel(text)
            label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            label.setProperty("muted", True)
            return label

        def _field_with_right_label(control: QWidget, text: str) -> QWidget:
            wrapper = QWidget()
            wrapper_layout = QHBoxLayout(wrapper)
            wrapper_layout.setContentsMargins(0, 0, 0, 0)
            wrapper_layout.setSpacing(6)
            wrapper_layout.addWidget(control, 1)
            wrapper_layout.addWidget(_right_label(text))
            return wrapper

        context_widget = QWidget()
        context_grid = QGridLayout(context_widget)
        context_grid.setContentsMargins(0, 0, 0, 0)
        context_grid.setHorizontalSpacing(8)
        context_grid.setVerticalSpacing(0)
        context_grid.addWidget(_field_with_right_label(self.context_window_spin, "上下文窗口"), 0, 0)
        context_grid.addWidget(_field_with_right_label(self.max_output_spin, "最大输出"), 0, 1)
        context_grid.setColumnStretch(0, 1)
        context_grid.setColumnStretch(1, 1)
        model_layout.addRow("Context:", context_widget)

        sampling_widget = QWidget()
        sampling_grid = QGridLayout(sampling_widget)
        sampling_grid.setContentsMargins(0, 0, 0, 0)
        sampling_grid.setHorizontalSpacing(8)
        sampling_grid.setVerticalSpacing(0)
        sampling_grid.addWidget(_field_with_right_label(self.temperature_spin, "Temperature"), 0, 0)
        sampling_grid.addWidget(_field_with_right_label(self.top_p_spin, "Top P"), 0, 1)
        sampling_grid.setColumnStretch(0, 1)
        sampling_grid.setColumnStretch(1, 1)
        model_layout.addRow("Sampling:", sampling_widget)

        reasoning_widget = QWidget()
        reasoning_grid = QGridLayout(reasoning_widget)
        reasoning_grid.setContentsMargins(0, 0, 0, 0)
        reasoning_grid.setHorizontalSpacing(8)
        reasoning_grid.setVerticalSpacing(6)
        reasoning_grid.addWidget(_field_with_right_label(self.reasoning_style_combo, "风格"), 0, 0, 1, 2)
        reasoning_grid.addWidget(_field_with_right_label(self.reasoning_effort_combo, "Effort"), 1, 0)
        reasoning_grid.addWidget(_field_with_right_label(self.thinking_budget_spin, "推理 Tokens"), 1, 1)
        reasoning_grid.setColumnStretch(0, 1)
        reasoning_grid.setColumnStretch(1, 1)
        model_layout.addRow("Reasoning:", reasoning_widget)

        profile_hint = QLabel("这里描述模型自身能力与默认采样参数；主/副/备用模型属于会话运行策略，不在此设置。")
        profile_hint.setWordWrap(True)
        profile_hint.setProperty("muted", True)
        model_layout.addRow("", profile_hint)

        basic_layout.addWidget(model_group)
        basic_layout.addStretch()

        tabs.addTab(basic_tab, "基本")

        # Advanced tab
        advanced_tab = QWidget()
        advanced_layout = QVBoxLayout(advanced_tab)
        advanced_layout.setContentsMargins(8, 8, 8, 8)
        advanced_layout.setSpacing(8)

        headers_group = QGroupBox("自定义请求头")
        headers_layout = QVBoxLayout(headers_group)

        headers_help = QLabel("添加自定义 HTTP 请求头")
        headers_help.setProperty("muted", True)
        headers_layout.addWidget(headers_help)

        self.headers_table = QTableWidget(0, 2)
        self.headers_table.setHorizontalHeaderLabels(["名称", "值"])
        self.headers_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.headers_table.setMaximumHeight(110)
        headers_layout.addWidget(self.headers_table)

        headers_btn_layout = QHBoxLayout()
        add_header_btn = QPushButton("添加")
        add_header_btn.clicked.connect(self._add_header_row)
        headers_btn_layout.addWidget(add_header_btn)
        remove_header_btn = QPushButton("删除")
        remove_header_btn.clicked.connect(self._remove_header_row)
        headers_btn_layout.addWidget(remove_header_btn)
        headers_btn_layout.addStretch()
        headers_layout.addLayout(headers_btn_layout)

        advanced_layout.addWidget(headers_group)

        format_group = QGroupBox("请求附加字段 (JSON)")
        format_layout = QVBoxLayout(format_group)

        format_help = QLabel("额外请求 JSON 字段，例如 OpenAI reasoning 或 Anthropic thinking 配置。")
        format_help.setProperty("muted", True)
        format_layout.addWidget(format_help)

        self.request_format_input = QTextEdit()
        self.request_format_input.setPlaceholderText('{"key": "value"}')
        self.request_format_input.setMaximumHeight(90)
        format_layout.addWidget(self.request_format_input)

        advanced_layout.addWidget(format_group)
        advanced_layout.addStretch()

        tabs.addTab(advanced_tab, "高级")
        layout.addWidget(tabs)

        # Test connection
        test_layout = QHBoxLayout()
        test_btn = QPushButton("测试连接")
        test_btn.clicked.connect(self._test_connection)
        test_layout.addWidget(test_btn)
        self.test_status = QLabel("")
        test_layout.addWidget(self.test_status)
        test_layout.addStretch()
        layout.addLayout(test_layout)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        save_btn = QPushButton("保存")
        save_btn.setProperty("primary", True)
        save_btn.clicked.connect(self._save)
        btn_layout.addWidget(save_btn)

        layout.addLayout(btn_layout)

    def _load_provider(self):
        self.name_input.setText(self.provider.name)
        api_type = str(getattr(self.provider, "api_type", OPENAI_COMPATIBLE) or OPENAI_COMPATIBLE)
        idx = self.api_type_combo.findData(api_type)
        self.api_type_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.api_base_input.setText(self.provider.api_base)
        self.api_key_input.setText(self.provider.api_key)
        self.enabled_check.setChecked(self.provider.enabled)

        self.model_combo.blockSignals(True)
        try:
            self.model_combo.clear()
            for model in self.provider.models:
                self.model_combo.addItem(model)
            if self.provider.default_model:
                idx = self.model_combo.findText(self.provider.default_model)
                if idx >= 0:
                    self.model_combo.setCurrentIndex(idx)
                else:
                    self.model_combo.setCurrentText(self.provider.default_model)
        finally:
            self.model_combo.blockSignals(False)

        if self.provider.models:
            display = ", ".join(self.provider.models[:8])
            if len(self.provider.models) > 8:
                display += f" ... (+{len(self.provider.models) - 8})"
            self.models_label.setText(display)

        for key, value in self.provider.custom_headers.items():
            self._add_header_row(key, value)

        if self.provider.request_format:
            self.request_format_input.setPlainText(
                json.dumps(self.provider.request_format, indent=2, ensure_ascii=False)
            )

        self._apply_api_type_presentation(api_type)
        self._load_current_model_profile_capabilities()

    def _on_model_changed(self, text: str = "") -> None:
        if self._loading_profile:
            return
        self._save_current_model_profile(self._active_profile_model_id)
        self._load_current_model_profile_capabilities(text)

    def _load_current_model_profile_capabilities(self, _text: str = "") -> None:
        if self._loading_profile:
            return
        model_id = self.model_combo.currentText().strip()
        profile = self.provider.find_model_profile(model_id) if model_id else None
        self._loading_profile = True
        try:
            supports_reasoning = bool(
                getattr(profile, "supports_reasoning", False)
                if profile is not None
                else getattr(self.provider, "supports_thinking", False)
            )
            supports_vision = bool(
                getattr(profile, "supports_vision", False)
                if profile is not None
                else getattr(self.provider, "supports_vision", True)
            )
            supports_tools = bool(getattr(profile, "supports_tools", True) if profile is not None else True)
            self.model_vision_check.setChecked(supports_vision)
            self.model_tools_check.setChecked(supports_tools)
            self.model_reasoning_check.setChecked(supports_reasoning)
            self.context_window_spin.setValue(int(getattr(profile, "context_window", 0) or 0))
            self.max_output_spin.setValue(int(getattr(profile, "max_output_tokens", 0) or 0))
            self.temperature_spin.setValue(float(getattr(profile, "default_temperature", 0.0) or 0.0))
            self.top_p_spin.setValue(float(getattr(profile, "default_top_p", 0.0) or 0.0))
            style = str(getattr(profile, "reasoning_style", "") or ("reasoning" if supports_reasoning else "none"))
            idx = self.reasoning_style_combo.findData(style)
            self.reasoning_style_combo.setCurrentIndex(idx if idx >= 0 else 0)
            effort = str(getattr(profile, "reasoning_effort", "") or "").strip().lower()
            idx = self.reasoning_effort_combo.findData(effort)
            if idx >= 0:
                self.reasoning_effort_combo.setCurrentIndex(idx)
            else:
                self.reasoning_effort_combo.setCurrentText(effort)
            self.thinking_budget_spin.setValue(int(getattr(profile, "thinking_budget_tokens", 0) or 0))
            self._active_profile_model_id = model_id
        finally:
            self._loading_profile = False

    def _save_current_model_profile(self, model_id: str | None = None) -> None:
        model_id = str(model_id if model_id is not None else self.model_combo.currentText()).strip()
        if not model_id:
            return

        existing = self.provider.find_model_profile(model_id)
        profile = ModelProfile.from_dict(existing.to_dict()) if existing is not None else ModelProfile.from_model_id(model_id)
        profile.model_id = model_id
        if not profile.display_name:
            profile.display_name = model_id
        profile.supports_vision = bool(self.model_vision_check.isChecked())
        profile.supports_tools = bool(self.model_tools_check.isChecked())
        profile.supports_reasoning = bool(self.model_reasoning_check.isChecked())
        context_window = int(self.context_window_spin.value() or 0)
        max_output_tokens = int(self.max_output_spin.value() or 0)
        profile.context_window = context_window if context_window > 0 else None
        profile.max_output_tokens = max_output_tokens if max_output_tokens > 0 else None
        temperature = float(self.temperature_spin.value() or 0.0)
        top_p = float(self.top_p_spin.value() or 0.0)
        profile.default_temperature = temperature if temperature > 0 else None
        profile.default_top_p = top_p if top_p > 0 else None
        profile.reasoning_style = str(self.reasoning_style_combo.currentData() or "none").strip().lower() or "none"
        effort = str(self.reasoning_effort_combo.currentData() or self.reasoning_effort_combo.currentText() or "").strip().lower()
        profile.reasoning_effort = "" if effort == "不设置" else effort
        budget = int(self.thinking_budget_spin.value() or 0)
        profile.thinking_budget_tokens = budget if budget > 0 else None

        profiles = [p for p in self.provider.model_profiles if getattr(p, "model_id", "") != model_id]
        profiles.append(profile)
        self.provider.model_profiles = profiles

    def _on_api_type_changed(self, _index: int) -> None:
        api_type = str(self.api_type_combo.currentData() or OPENAI_COMPATIBLE)
        self._apply_api_type_presentation(api_type)

    def _apply_api_type_presentation(self, api_type: str) -> None:
        normalized = str(api_type or OPENAI_COMPATIBLE).strip().lower() or OPENAI_COMPATIBLE
        if normalized == ANTHROPIC_NATIVE:
            self.api_type_help.setText(
                "Anthropic 原生接口会自动使用 `x-api-key`、`anthropic-version` 头，并把请求发送到 `/messages`。"
            )
            self.api_base_input.setPlaceholderText("https://api.anthropic.com/v1")
            self.api_key_input.setPlaceholderText("sk-ant-...")
            self.request_format_input.setPlaceholderText('{"thinking": {"type": "enabled", "budget_tokens": 2048}}')
            if self.api_base_input.text().strip() in {"", "https://api.openai.com/v1"}:
                self.api_base_input.setText("https://api.anthropic.com/v1")
        elif normalized == OPENAI_RESPONSES:
            self.api_type_help.setText(
                "OpenAI Responses 接口会使用 `Authorization: Bearer ...`，并把请求发送到 `/responses`。"
            )
            self.api_base_input.setPlaceholderText("https://api.openai.com/v1")
            self.api_key_input.setPlaceholderText("sk-...")
            self.request_format_input.setPlaceholderText('{"reasoning": {"effort": "medium"}}')
            if self.api_base_input.text().strip() in {"", "https://api.anthropic.com/v1", "http://localhost:11434"}:
                self.api_base_input.setText("https://api.openai.com/v1")
        elif normalized == OLLAMA_CHAT:
            self.api_type_help.setText(
                "Ollama 本地接口会把请求发送到 `/api/chat`，模型列表读取 `/api/tags`，通常不需要 API Key。"
            )
            self.api_base_input.setPlaceholderText("http://localhost:11434")
            self.api_key_input.setPlaceholderText("可留空")
            self.request_format_input.setPlaceholderText('{"options": {"num_ctx": 8192}}')
            if self.api_base_input.text().strip() in {"", "https://api.openai.com/v1", "https://api.anthropic.com/v1"}:
                self.api_base_input.setText("http://localhost:11434")
        else:
            self.api_type_help.setText(
                "OpenAI 兼容接口会自动使用 `Authorization: Bearer ...`，并把请求发送到 `/chat/completions`。"
            )
            self.api_base_input.setPlaceholderText("https://api.openai.com/v1")
            self.api_key_input.setPlaceholderText("sk-...")
            self.request_format_input.setPlaceholderText('{"reasoning": {"effort": "medium"}}')
            if self.api_base_input.text().strip() in {"", "https://api.anthropic.com/v1"}:
                self.api_base_input.setText("https://api.openai.com/v1")

    def _add_header_row(self, key: str = "", value: str = ""):
        row = self.headers_table.rowCount()
        self.headers_table.insertRow(row)
        self.headers_table.setItem(row, 0, QTableWidgetItem(key))
        self.headers_table.setItem(row, 1, QTableWidgetItem(value))

    def _remove_header_row(self):
        row = self.headers_table.currentRow()
        if row >= 0:
            self.headers_table.removeRow(row)

    def _fetch_models(self):
        self._save_to_provider()

        valid, msg = self.provider_service.validate_provider(self.provider)
        if not valid:
            QMessageBox.warning(self, "验证错误", msg)
            return

        self.models_label.setText("正在获取模型...")

        async def fetch():
            return await self.provider_service.fetch_models(self.provider)

        try:
            loop = asyncio.new_event_loop()
            models = loop.run_until_complete(fetch())
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

            if models:
                self.provider.models = models
                current = self.model_combo.currentText()
                self.model_combo.clear()
                for model in models:
                    self.model_combo.addItem(model)
                idx = self.model_combo.findText(current)
                if idx >= 0:
                    self.model_combo.setCurrentIndex(idx)

                display = ", ".join(models[:8])
                if len(models) > 8:
                    display += f" ... (+{len(models) - 8})"
                self.models_label.setText(display)
            else:
                self.models_label.setText("未找到模型")
        except Exception as e:
            self.models_label.setText(f"错误: {str(e)[:50]}")

    def _test_connection(self):
        self._save_to_provider()

        valid, msg = self.provider_service.validate_provider(self.provider)
        if not valid:
            self.test_status.setText(f"连接失败：{msg}")
            self.test_status.setStyleSheet("color: #ef4444;")
            return

        self.test_status.setText("测试中...")
        self.test_status.setStyleSheet("color: #a1a1aa;")

        async def test():
            return await self.provider_service.test_connection(self.provider)

        try:
            loop = asyncio.new_event_loop()
            success, message = loop.run_until_complete(test())
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

            if success:
                self.test_status.setText("连接成功")
                self.test_status.setStyleSheet("color: #22c55e;")
            else:
                self.test_status.setText(f"连接失败：{message}")
                self.test_status.setStyleSheet("color: #ef4444;")
        except Exception as e:
            self.test_status.setText(f"连接失败：{str(e)[:30]}")
            self.test_status.setStyleSheet("color: #ef4444;")

    def _save_to_provider(self):
        self.provider.name = self.name_input.text().strip()
        self.provider.api_type = str(self.api_type_combo.currentData() or OPENAI_COMPATIBLE)
        self.provider.api_base = self.api_base_input.text().strip()
        self.provider.api_key = self.api_key_input.text().strip()
        self.provider.default_model = self.model_combo.currentText().strip()
        self.provider.supports_vision = self.model_vision_check.isChecked()
        self.provider.supports_thinking = self.model_reasoning_check.isChecked()
        self.provider.enabled = self.enabled_check.isChecked()
        self._save_current_model_profile()

        headers = {}
        for row in range(self.headers_table.rowCount()):
            key_item = self.headers_table.item(row, 0)
            value_item = self.headers_table.item(row, 1)
            if key_item and value_item:
                key = key_item.text().strip()
                value = value_item.text().strip()
                if key:
                    headers[key] = value
        self.provider.custom_headers = headers

        format_text = self.request_format_input.toPlainText().strip()
        if format_text:
            try:
                self.provider.request_format = json.loads(format_text)
                self._request_format_parse_error = None
            except Exception as e:
                self.provider.request_format = {}
                self._request_format_parse_error = str(e)
        else:
            self.provider.request_format = {}
            self._request_format_parse_error = None

        self.provider.normalize_inplace()

    def _save(self):
        self._save_to_provider()

        if getattr(self, '_request_format_parse_error', None):
            QMessageBox.warning(
                self,
                "请求附加字段 JSON 无效",
                "请求附加字段 (JSON) 解析失败，将不会应用这些字段。\n\n"
                f"错误: {self._request_format_parse_error}"
            )
            return

        valid, msg = self.provider_service.validate_provider(self.provider)
        if not valid:
            QMessageBox.warning(self, "验证错误", msg)
            return

        self.accept()

    def get_provider(self) -> Provider:
        return self.provider


