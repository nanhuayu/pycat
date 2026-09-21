"""Compact editor for one provider-scoped :class:`ModelProfile`."""
from __future__ import annotations

import json

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from pycat.gui.settings.components import build_dialog_button_box
from pycat.gui.utils.combo_box import configure_combo_popup
from pycat.gui.utils.form_builder import FormSection
from pycat.gui.widgets.themed_line_edit import ThemedTextEdit
from pycat.models.model_profile import (
    DEFAULT_REASONING_OPTIONS,
    REASONING_MODES,
    ModelProfile,
)
from pycat.models.model_profile import reasoning_codecs_for_provider as _reasoning_codecs_for_provider
from pycat.models.provider import OPENAI_COMPATIBLE, Provider

_STRUCTURAL_REQUEST_FIELDS = {
    "model",
    "messages",
    "input",
    "instructions",
    "system",
    "tools",
    "stream",
}
_COMMON_REQUEST_FIELDS = {
    "temperature",
    "top_p",
    "max_tokens",
    "max_output_tokens",
    "reasoning",
    "reasoning_effort",
    "thinking",
    "think",
    "enable_thinking",
    "thinking_budget",
    "output_config",
    "options",
}
_OUTPUT_BUDGET_FIELDS = {"max_tokens", "max_output_tokens"}
_CODEC_LABELS = {
    "none": "无（使用接口默认）",
    "responses_effort": "Responses 推理强度",
    "chat_reasoning": "兼容接口推理（含 OpenRouter）",
    "chat_thinking_effort": "兼容接口 thinking + effort",
    "chat_toggle_budget": "兼容接口 thinking 开关",
    "anthropic_adaptive": "Anthropic adaptive",
    "ollama_think": "Ollama think",
}


def reasoning_codecs_for_provider(provider: Provider) -> tuple[str, ...]:
    """Return the small codec whitelist for one endpoint envelope.

    OpenRouter is a route through the OpenAI-compatible envelope, so it uses
    the same one ``chat_reasoning`` owner rather than a provider codec.
    """

    api_type = str(getattr(provider, "api_type", "") or OPENAI_COMPATIBLE).strip().lower()
    provider_key = str(getattr(provider, "catalog_key", "") or "").strip().lower()
    provider_name = str(getattr(provider, "canonical_name", "") or "").strip().lower()
    if provider_name == "openrouter":
        provider_key = "openrouter"
    return _reasoning_codecs_for_provider(api_type, provider_key)


class ModelProfileDialog(QDialog):
    """Edit one model without mutating the provider until the dialog is saved."""

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
        self._loading = False
        # Keep the exact declaration from an existing profile.  A profile may
        # intentionally expose a small subset (for example inherit/off/high)
        # even though the codec has a larger default set.  Re-saving without
        # changing the codec must not silently widen that contract.
        self._loaded_reasoning_codec = "none"
        self._declared_reasoning_options: list[str] | None = None
        self._reasoning_codec_changed = False
        self._setup_ui()
        self._load_profile(self._initial_profile())

    def _initial_profile(self) -> ModelProfile:
        if self._original_profile is not None:
            return ModelProfile.from_dict(self._original_profile.to_dict())
        if self._original_model_id:
            return self._provider.effective_model_profile(self._original_model_id)
        return self._provider.model_profile_template()

    def _setup_ui(self) -> None:
        self.setWindowTitle("添加模型" if not self._original_model_id else "编辑模型")
        self.setObjectName("model_profile_dialog")
        self.setMinimumSize(560, 500)
        self.resize(620, 590)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        self.tabs = QTabWidget()
        for title, content in (("基本", self._build_basic_tab()), ("高级", self._build_advanced_tab())):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidget(content)
            self.tabs.addTab(scroll, title)
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
        self.model_id_input = identity.add_line_edit("模型 ID", placeholder="实际发送给接口的完整 ID")
        self.model_id_input.setReadOnly(bool(self._original_model_id))
        self.model_id_input.textEdited.connect(self._suggest_model_type)
        self.display_name_input = identity.add_line_edit("显示名称", placeholder="可选")
        self.model_type_combo = QComboBox()
        self.model_type_combo.addItem("聊天与视觉理解", "chat")
        self.model_type_combo.addItem("图像生成与编辑", "image")
        configure_combo_popup(self.model_type_combo)
        identity.form.addRow("模型用途", self.model_type_combo)
        for editor in (self.model_id_input, self.display_name_input):
            editor.setMinimumHeight(30)
        layout.addWidget(identity.group)

        self.image_connection_note = QLabel("接口与认证由所属服务连接统一管理；尺寸、质量和数量在图像能力中设置。")
        self.image_connection_note.setWordWrap(True)
        self.image_connection_note.setProperty("muted", True)
        layout.addWidget(self.image_connection_note)

        capability = FormSection("能力")
        abilities = QWidget()
        abilities_layout = QHBoxLayout(abilities)
        abilities_layout.setContentsMargins(0, 0, 0, 0)
        abilities_layout.setSpacing(14)
        self.tools_check = QCheckBox("工具调用")
        self.reasoning_check = QCheckBox("推理")
        self.image_input_check = QCheckBox("图片输入")
        self.audio_input_check = QCheckBox("音频输入")
        self.audio_input_check.setToolTip("用于模型能力档案；当前输入框尚不发送音频附件。")
        for checkbox in (
            self.tools_check,
            self.reasoning_check,
            self.image_input_check,
            self.audio_input_check,
        ):
            abilities_layout.addWidget(checkbox)
        abilities_layout.addStretch(1)
        capability.form.addRow("支持", abilities)
        layout.addWidget(capability.group)

        generation = FormSection("生成")
        self.context_window_spin = self._optional_spin(10_000_000, 8192)
        self.max_output_spin = self._optional_spin(1_000_000, 1024)
        self.temperature_spin = self._optional_double_spin(2.0)
        self.top_p_spin = self._optional_double_spin(1.0)
        generation_fields = QWidget()
        generation_grid = QGridLayout(generation_fields)
        generation_grid.setContentsMargins(0, 0, 0, 0)
        generation_grid.setHorizontalSpacing(8)
        generation_grid.setVerticalSpacing(6)
        for column in (1, 3):
            generation_grid.setColumnStretch(column, 1)
        context_window_label = QLabel("总窗口")
        context_window_tooltip = "模型总上下文窗口，输入与输出共享。"
        context_window_label.setToolTip(context_window_tooltip)
        self.context_window_spin.setToolTip(context_window_tooltip)
        generation_grid.addWidget(context_window_label, 0, 0)
        generation_grid.addWidget(self.context_window_spin, 0, 1)
        max_output_label = QLabel("最大输出")
        max_output_tooltip = "模型档案声明的单次输出上限；运行时会与会话请求和总窗口共同校准。"
        max_output_label.setToolTip(max_output_tooltip)
        self.max_output_spin.setToolTip(max_output_tooltip)
        generation_grid.addWidget(max_output_label, 0, 2)
        generation_grid.addWidget(self.max_output_spin, 0, 3)
        generation_grid.addWidget(QLabel("Temperature"), 1, 0)
        generation_grid.addWidget(self.temperature_spin, 1, 1)
        generation_grid.addWidget(QLabel("Top P"), 1, 2)
        generation_grid.addWidget(self.top_p_spin, 1, 3)
        generation.form.addRow(generation_fields)
        # Paired numeric fields share one row instead of each taking the
        # settings form's full-width 200 px editor minimum.
        for spin in (self.context_window_spin, self.max_output_spin, self.temperature_spin, self.top_p_spin):
            spin.setMinimumWidth(100)
        layout.addWidget(generation.group)

        reasoning = FormSection("推理")
        self.reasoning_default_combo = QComboBox()
        self.reasoning_default_combo.setMinimumHeight(30)
        self.reasoning_default_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        configure_combo_popup(self.reasoning_default_combo)
        reasoning.form.addRow("默认推理", self.reasoning_default_combo)
        self.reasoning_codec_combo = QComboBox()
        self.reasoning_codec_combo.setMinimumHeight(30)
        self.reasoning_codec_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        configure_combo_popup(self.reasoning_codec_combo)
        reasoning.form.addRow("推理协议", self.reasoning_codec_combo)
        self.reasoning_note = QLabel("")
        self.reasoning_note.setWordWrap(True)
        self.reasoning_note.setProperty("muted", True)
        reasoning.form.addRow(self.reasoning_note, info=True)
        layout.addWidget(reasoning.group)
        self._chat_groups = (capability.group, generation.group, reasoning.group)
        self.model_type_combo.currentIndexChanged.connect(self._sync_model_type)

        self.reasoning_check.toggled.connect(self._sync_reasoning_controls)
        self.reasoning_codec_combo.currentIndexChanged.connect(self._on_codec_changed)
        layout.addStretch(1)
        return tab

    def _sync_model_type(self) -> None:
        chat = self.model_type_combo.currentData() == "chat"
        self.image_connection_note.setVisible(not chat)
        for group in self._chat_groups:
            group.setVisible(chat)
        self._update_extra_body_warning()

    def _suggest_model_type(self, text: str) -> None:
        if not self._original_model_id and text.strip().startswith(("gpt-image-", "chatgpt-image-")):
            self.model_type_combo.setCurrentIndex(self.model_type_combo.findData("image"))

    def _build_advanced_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        advanced = FormSection("附加请求")
        self.custom_headers_edit = self._json_editor('{"X-Model-Header": "value"}')
        self.custom_headers_edit.setToolTip(
            "仅附加到当前模型；普通同名字段覆盖 Provider 请求头，认证和传输保留头会被忽略。"
        )
        advanced.form.addRow("自定义请求头", self.custom_headers_edit)
        self.extra_body_edit = self._json_editor('{"service_tier": "priority"}')
        self.extra_body_edit.setToolTip(
            "在 envelope 与推理协议之后做一次顶层覆盖；用于未内置的 Provider 私有字段。"
        )
        self.extra_body_warning = QLabel("")
        self.extra_body_warning.setWordWrap(True)
        self.extra_body_warning.setProperty("muted", True)
        advanced.form.addRow("请求字段", self.extra_body_edit)
        advanced.form.addRow("", self.extra_body_warning)
        layout.addWidget(advanced.group, 1)

        self.extra_body_edit.textChanged.connect(self._update_extra_body_warning)
        return tab

    @staticmethod
    def _optional_spin(maximum: int, step: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setMinimumSize(100, 30)
        spin.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        spin.setRange(0, maximum)
        spin.setSingleStep(step)
        spin.setSpecialValueText("未设置")
        return spin

    @staticmethod
    def _optional_double_spin(maximum: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setMinimumSize(100, 30)
        spin.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        spin.setRange(-0.01, maximum)
        spin.setDecimals(2)
        spin.setSingleStep(0.05)
        spin.setSpecialValueText("未设置")
        spin.setValue(-0.01)
        return spin

    @staticmethod
    def _json_editor(placeholder: str) -> QTextEdit:
        editor = ThemedTextEdit()
        editor.setAcceptRichText(False)
        editor.setPlaceholderText(placeholder)
        editor.setMaximumHeight(110)
        return editor

    def _load_profile(self, profile: ModelProfile) -> None:
        self._loading = True
        self._reasoning_codec_changed = False
        try:
            self.model_id_input.setText(self._original_model_id or profile.model_id)
            self.display_name_input.setText(profile.display_name)
            self.model_type_combo.setCurrentIndex(max(0, self.model_type_combo.findData(profile.model_type)))
            self.tools_check.setChecked(profile.supports_tools)
            self.reasoning_check.setChecked(profile.supports_reasoning)
            self.image_input_check.setChecked(profile.supports_input("image"))
            self.audio_input_check.setChecked(profile.supports_input("audio"))
            self.context_window_spin.setValue(profile.context_window or 0)
            self.max_output_spin.setValue(profile.max_output_tokens or 0)
            self.temperature_spin.setValue(
                profile.default_temperature if profile.default_temperature is not None else -0.01
            )
            self.top_p_spin.setValue(profile.default_top_p if profile.default_top_p is not None else -0.01)
            self._populate_codecs(profile.reasoning_codec)
            selected_codec = str(self.reasoning_codec_combo.currentData() or "none")
            self._loaded_reasoning_codec = selected_codec
            self._declared_reasoning_options = (
                list(profile.reasoning_options)
                if selected_codec == profile.reasoning_codec and profile.reasoning_codec != "none"
                else None
            )
            self._populate_reasoning_defaults(
                selected_codec,
                profile.reasoning_options if selected_codec == profile.reasoning_codec else None,
                profile.reasoning_default,
            )
            self.extra_body_edit.setPlainText(self._json_text(profile.extra_body))
            self.custom_headers_edit.setPlainText(self._json_text(profile.custom_headers))
        finally:
            self._loading = False
        self._sync_reasoning_controls()
        self._sync_model_type()
        self._update_extra_body_warning()

    def _populate_codecs(self, preferred: str = "none") -> None:
        options = reasoning_codecs_for_provider(self._provider)
        current = str(preferred or "none").strip().lower()
        if current not in options:
            current = "none"
        self.reasoning_codec_combo.blockSignals(True)
        try:
            self.reasoning_codec_combo.clear()
            for codec in options:
                self.reasoning_codec_combo.addItem(_CODEC_LABELS.get(codec, codec), codec)
                index = self.reasoning_codec_combo.count() - 1
                self.reasoning_codec_combo.setItemData(index, codec)
                self.reasoning_codec_combo.setItemData(index, codec, Qt.ItemDataRole.ToolTipRole)
            index = self.reasoning_codec_combo.findData(current)
            self.reasoning_codec_combo.setCurrentIndex(index if index >= 0 else 0)
        finally:
            self.reasoning_codec_combo.blockSignals(False)

    def _profile_reasoning_options(self, codec: str, existing: list[str] | None = None) -> list[str]:
        allowed = list(DEFAULT_REASONING_OPTIONS.get(codec, ("inherit",)))
        if existing:
            declared = [mode for mode in existing if mode in REASONING_MODES]
            if declared:
                allowed = [mode for mode in allowed if mode in declared]
                if "inherit" not in allowed:
                    allowed.insert(0, "inherit")
        return allowed or ["inherit"]

    def _populate_reasoning_defaults(
        self,
        codec: str,
        existing_options: list[str] | None = None,
        preferred: str = "inherit",
    ) -> None:
        codec = str(codec or "none")
        options = ["inherit"] if codec == "none" else self._profile_reasoning_options(codec, existing_options)
        selected = str(preferred or "inherit").strip().lower()
        if selected not in options:
            selected = "inherit"
        self.reasoning_default_combo.blockSignals(True)
        try:
            self.reasoning_default_combo.clear()
            labels = {
                "inherit": "接口默认",
                "off": "关闭",
                "on": "开启",
                "auto": "自动",
            }
            for mode in options:
                self.reasoning_default_combo.addItem(labels.get(mode, mode), mode)
            self.reasoning_default_combo.setCurrentIndex(
                max(0, self.reasoning_default_combo.findData(selected))
            )
        finally:
            self.reasoning_default_combo.blockSignals(False)
        self.reasoning_note.setText(
            "未发送显式推理字段（接口默认）。"
            if codec == "none"
            else f"可用模式：{'、'.join(options)}"
        )

    def _on_codec_changed(self, _index: int) -> None:
        if self._loading:
            return
        self._reasoning_codec_changed = True
        codec = str(self.reasoning_codec_combo.currentData() or "none")
        self._populate_reasoning_defaults(codec)
        self._sync_reasoning_controls()

    def _sync_reasoning_controls(self, *_args) -> None:
        enabled = self.reasoning_check.isChecked()
        codec = str(self.reasoning_codec_combo.currentData() or "none")
        active = enabled and codec != "none"
        self.reasoning_codec_combo.setEnabled(enabled)
        self.reasoning_default_combo.setEnabled(active)

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

    def _update_extra_body_warning(self) -> None:
        if self.model_type_combo.currentData() == "image":
            self.extra_body_warning.setText("仅填写所选图像协议支持的附加参数；不能覆盖模型、提示词、输入图片与图像参数。Qwen 原生参数放在 parameters 对象中。")
            return
        text = self.extra_body_edit.toPlainText().strip()
        if not text:
            self.extra_body_warning.clear()
            return
        try:
            value = json.loads(text)
        except Exception:
            self.extra_body_warning.setText("JSON 尚未完成。")
            return
        if not isinstance(value, dict):
            self.extra_body_warning.setText("请求字段必须是 JSON 对象。")
            return
        structural = sorted(set(value).intersection(_STRUCTURAL_REQUEST_FIELDS))
        budget_fields = sorted(set(value).intersection(_OUTPUT_BUDGET_FIELDS))
        overrides = sorted(
            set(value).intersection(_COMMON_REQUEST_FIELDS).difference(_OUTPUT_BUDGET_FIELDS)
        )
        parts: list[str] = []
        if structural:
            parts.append("发送时忽略结构字段：" + ", ".join(structural))
        if budget_fields:
            parts.append("作为输出预算输入并在发送前校准：" + ", ".join(budget_fields))
        if overrides:
            parts.append("将覆盖生成/推理字段：" + ", ".join(overrides))
        self.extra_body_warning.setText("；".join(parts))

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
        supports_reasoning = self.reasoning_check.isChecked()
        codec = str(self.reasoning_codec_combo.currentData() or "none") if supports_reasoning else "none"
        if (
            supports_reasoning
            and codec != "none"
            and not self._reasoning_codec_changed
            and codec == self._loaded_reasoning_codec
            and self._declared_reasoning_options
        ):
            options = list(self._declared_reasoning_options)
        else:
            options = ["inherit"] if codec == "none" else self._profile_reasoning_options(codec)
        reasoning_default = str(self.reasoning_default_combo.currentData() or "inherit")
        if reasoning_default not in options:
            reasoning_default = "inherit"
        input_modalities = ["text"]
        if self.image_input_check.isChecked():
            input_modalities.append("image")
        if self.audio_input_check.isChecked():
            input_modalities.append("audio")
        payload.update(
            {
                "model_id": self.model_id(),
                "model_type": self.model_type_combo.currentData(),
                "display_name": self.display_name_input.text().strip(),
                "supports_tools": self.tools_check.isChecked(),
                "supports_reasoning": supports_reasoning,
                "input_modalities": input_modalities,
                "reasoning_codec": codec,
                "reasoning_options": options,
                "reasoning_default": reasoning_default,
                "context_window": self.context_window_spin.value() or None,
                "max_output_tokens": self.max_output_spin.value() or None,
                "default_temperature": (
                    self.temperature_spin.value() if self.temperature_spin.value() >= 0 else None
                ),
                "default_top_p": self.top_p_spin.value() if self.top_p_spin.value() >= 0 else None,
                "custom_headers": self._json_object(self.custom_headers_edit, "自定义请求头"),
                "extra_body": self._json_object(self.extra_body_edit, "请求字段"),
            }
        )
        return ModelProfile.from_dict(payload).as_user_managed()

    def accepted_profile(self) -> ModelProfile:
        return ModelProfile.from_dict((self._accepted_profile or self._collect_profile()).to_dict())
