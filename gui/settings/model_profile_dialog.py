"""Focused editor for one curated provider model."""
from __future__ import annotations

import json

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QHBoxLayout,
    QMessageBox,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from gui.settings.components import build_dialog_button_box
from gui.utils.combo_box import configure_combo_popup
from gui.utils.form_builder import FormSection
from models.model_profile import ModelProfile
from models.provider import Provider


class ModelProfileDialog(QDialog):
    """Edit one ``ModelProfile`` without mutating its provider."""

    def __init__(
        self,
        provider: Provider,
        *,
        model_id: str = "",
        profile: ModelProfile | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._provider = Provider.from_dict(provider.to_dict())
        self._original_model_id = str(model_id or "").strip()
        self._original_profile = ModelProfile.from_dict(profile.to_dict()) if profile is not None else None
        self._accepted_profile: ModelProfile | None = None
        self._setup_ui()
        self._load_profile(self._initial_profile())

    def _initial_profile(self) -> ModelProfile:
        if self._original_profile is not None:
            return ModelProfile.from_dict(self._original_profile.to_dict())
        if self._original_model_id:
            return self._provider.effective_model_profile(self._original_model_id)
        return ModelProfile.from_model_id(
            "",
            supports_vision=bool(self._provider.supports_vision),
            supports_reasoning=bool(self._provider.supports_reasoning),
        )

    def _setup_ui(self) -> None:
        self.setWindowTitle("添加模型" if not self._original_model_id else "编辑模型")
        self.setObjectName("model_profile_dialog")
        self.setMinimumSize(520, 500)
        self.resize(560, 560)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_basic_tab(), "基本")
        self.tabs.addTab(self._build_generation_tab(), "生成")
        self.tabs.addTab(self._build_reasoning_tab(), "推理与高级")
        root.addWidget(self.tabs, 1)

        buttons = build_dialog_button_box(self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _build_basic_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        identity = FormSection("模型")
        self.model_id_input = identity.add_line_edit("模型 ID", placeholder="例如 gpt-4.1")
        self.model_id_input.setReadOnly(bool(self._original_model_id))
        self.display_name_input = identity.add_line_edit("显示名称", placeholder="可选")
        layout.addWidget(identity.group)

        capability = FormSection("能力")
        abilities = QWidget()
        abilities_layout = QHBoxLayout(abilities)
        abilities_layout.setContentsMargins(0, 0, 0, 0)
        abilities_layout.setSpacing(14)
        self.tools_check = QCheckBox("工具")
        self.vision_check = QCheckBox("视觉")
        self.reasoning_check = QCheckBox("推理")
        for checkbox in (self.tools_check, self.vision_check, self.reasoning_check):
            abilities_layout.addWidget(checkbox)
        abilities_layout.addStretch(1)
        capability.form.addRow("支持", abilities)
        layout.addWidget(capability.group)
        layout.addStretch(1)
        return tab

    def _build_generation_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(10, 10, 10, 10)

        generation = FormSection("生成参数")
        self.context_window_spin = self._optional_spin(2_000_000, 8192)
        self.max_output_spin = self._optional_spin(200_000, 1024)
        self.temperature_spin = self._optional_double_spin(2.0)
        self.top_p_spin = self._optional_double_spin(1.0)
        generation.form.addRow("上下文窗口", self.context_window_spin)
        generation.form.addRow("最大输出", self.max_output_spin)
        generation.form.addRow("Temperature", self.temperature_spin)
        generation.form.addRow("Top P", self.top_p_spin)
        layout.addWidget(generation.group)
        layout.addStretch(1)
        return tab

    def _build_reasoning_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        reasoning = FormSection("推理")
        self.reasoning_default_combo = QComboBox()
        self.reasoning_default_combo.addItem("继承接口默认", None)
        self.reasoning_default_combo.addItem("默认开启", True)
        self.reasoning_default_combo.addItem("默认关闭", False)
        configure_combo_popup(self.reasoning_default_combo)

        self.reasoning_effort_combo = QComboBox()
        self.reasoning_effort_combo.setEditable(True)
        for label, value in (
            ("继承接口默认", ""),
            ("low", "low"),
            ("medium", "medium"),
            ("high", "high"),
            ("max", "max"),
            ("xhigh", "xhigh"),
        ):
            self.reasoning_effort_combo.addItem(label, value)
        configure_combo_popup(self.reasoning_effort_combo)
        reasoning.form.addRow("默认状态", self.reasoning_default_combo)
        reasoning.form.addRow("推理强度", self.reasoning_effort_combo)
        layout.addWidget(reasoning.group)

        advanced = FormSection("附加字段")
        self.request_overrides_edit = self._json_editor('{"service_tier": "auto"}')
        self.request_overrides_edit.setToolTip("非标准接口的附加请求字段；模型、消息、工具和生成核心字段不会被覆盖。")
        advanced.form.addRow("请求字段", self.request_overrides_edit)
        layout.addWidget(advanced.group, 1)

        self.reasoning_check.toggled.connect(self._sync_reasoning_controls)
        self.reasoning_default_combo.currentIndexChanged.connect(self._on_reasoning_default_changed)
        return tab

    @staticmethod
    def _optional_spin(maximum: int, step: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setMinimumWidth(180)
        spin.setRange(0, maximum)
        spin.setSingleStep(step)
        spin.setSpecialValueText("不设置")
        return spin

    @staticmethod
    def _optional_double_spin(maximum: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setMinimumWidth(180)
        spin.setRange(-0.01, maximum)
        spin.setDecimals(2)
        spin.setSingleStep(0.05)
        spin.setSpecialValueText("不设置")
        spin.setValue(-0.01)
        return spin

    @staticmethod
    def _json_editor(placeholder: str) -> QTextEdit:
        editor = QTextEdit()
        editor.setAcceptRichText(False)
        editor.setPlaceholderText(placeholder)
        editor.setMaximumHeight(86)
        return editor

    def _load_profile(self, profile: ModelProfile) -> None:
        self.model_id_input.setText(self._original_model_id or profile.model_id)
        self.display_name_input.setText(profile.display_name)
        self.tools_check.setChecked(profile.supports_tools)
        self.vision_check.setChecked(profile.supports_vision)
        self.reasoning_check.setChecked(profile.supports_reasoning)
        self.context_window_spin.setValue(profile.context_window or 0)
        self.max_output_spin.setValue(profile.max_output_tokens or 0)
        self.temperature_spin.setValue(profile.default_temperature if profile.default_temperature is not None else -0.01)
        self.top_p_spin.setValue(profile.default_top_p if profile.default_top_p is not None else -0.01)
        default_index = self.reasoning_default_combo.findData(profile.reasoning_enabled)
        self.reasoning_default_combo.setCurrentIndex(default_index if default_index >= 0 else 0)
        effort_index = self.reasoning_effort_combo.findData(profile.reasoning_effort)
        if effort_index >= 0:
            self.reasoning_effort_combo.setCurrentIndex(effort_index)
        else:
            self.reasoning_effort_combo.setCurrentText(profile.reasoning_effort)
        self.request_overrides_edit.setPlainText(self._json_text(profile.request_overrides))
        self._sync_reasoning_controls()

    @staticmethod
    def _json_text(value: dict) -> str:
        return json.dumps(value, ensure_ascii=False, indent=2) if value else ""

    @staticmethod
    def _json_object(editor: QTextEdit, label: str) -> dict:
        text = editor.toPlainText().strip()
        if not text:
            return {}
        try:
            value = json.loads(text)
        except Exception as exc:
            raise ValueError(f"{label} JSON 无效：{exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{label}必须是 JSON 对象")
        return value

    def _sync_reasoning_controls(self) -> None:
        enabled = self.reasoning_check.isChecked()
        self.reasoning_default_combo.setEnabled(enabled)
        self.reasoning_effort_combo.setEnabled(
            enabled and self.reasoning_default_combo.currentData() is not False
        )

    def _on_reasoning_default_changed(self, _index: int) -> None:
        if self.reasoning_default_combo.currentData() is False:
            self.reasoning_effort_combo.setCurrentIndex(0)
        self._sync_reasoning_controls()

    def accept(self) -> None:
        if not self.model_id():
            QMessageBox.warning(self, "模型 ID 无效", "请输入模型 ID。")
            self.model_id_input.setFocus()
            return
        try:
            self._accepted_profile = self._collect_profile()
        except ValueError as exc:
            QMessageBox.warning(self, "模型参数无效", str(exc))
            return
        super().accept()

    def model_id(self) -> str:
        return str(self.model_id_input.text() or "").strip()

    def _collect_profile(self) -> ModelProfile:
        base = self._original_profile or ModelProfile(model_id=self.model_id())
        payload = base.to_dict()
        effort_data = self.reasoning_effort_combo.currentData()
        effort_text = self.reasoning_effort_combo.currentText().strip()
        effort = str(effort_data or effort_text).strip().lower()
        if not effort_data and effort_text == self.reasoning_effort_combo.itemText(0):
            effort = ""
        supports_reasoning = self.reasoning_check.isChecked()
        reasoning_enabled = self.reasoning_default_combo.currentData() if supports_reasoning else None
        if not supports_reasoning or reasoning_enabled is False:
            effort = ""
        payload.update(
            {
                "model_id": self.model_id(),
                "display_name": self.display_name_input.text().strip(),
                "supports_tools": self.tools_check.isChecked(),
                "supports_vision": self.vision_check.isChecked(),
                "supports_reasoning": supports_reasoning,
                "reasoning_enabled": reasoning_enabled,
                "context_window": self.context_window_spin.value() or None,
                "max_output_tokens": self.max_output_spin.value() or None,
                "default_temperature": self.temperature_spin.value() if self.temperature_spin.value() >= 0 else None,
                "default_top_p": self.top_p_spin.value() if self.top_p_spin.value() >= 0 else None,
                "reasoning_effort": effort,
                "request_overrides": self._json_object(self.request_overrides_edit, "请求字段"),
            }
        )
        return ModelProfile.from_dict(payload)

    def accepted_profile(self) -> ModelProfile:
        return ModelProfile.from_dict((self._accepted_profile or self._collect_profile()).to_dict())
