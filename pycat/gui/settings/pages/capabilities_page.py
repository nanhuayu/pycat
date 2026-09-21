"""Settings editor for single-call LLM capabilities."""
from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Iterable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from pycat.core.capabilities import (
    CapabilitiesConfig,
    CapabilitiesManager,
    CapabilityConfig,
    default_capabilities_config,
)
from pycat.gui.settings.components import (
    SettingsActionBar,
    SettingsListDetailLayout,
    SettingsStatusListItem,
    configure_settings_resource_list,
)
from pycat.gui.settings.page_header import build_page_header
from pycat.gui.utils.combo_box import configure_combo_popup
from pycat.gui.utils.form_builder import FormSection
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.settings_controls import SettingsFormLayout, SettingsToggle
from pycat.gui.widgets.model_ref_selector import ModelTargetCombo, build_model_ref_options
from pycat.gui.widgets.themed_line_edit import ThemedLineEdit, ThemedTextEdit
from pycat.models.contracts.capability import ImageGenerationOptions
from pycat.models.contracts.config import PromptsConfig
from pycat.models.contracts.model_target import ModelTarget
from pycat.models.contracts.tooling import TOOL_CATEGORIES, TOOL_CATEGORY_LABELS
from pycat.models.provider import Provider


class CapabilitiesPage(QWidget):
    page_title = "能力"

    def __init__(
        self,
        prompts: PromptsConfig,
        *,
        providers: Iterable[Provider] | None = None,
        capabilities: CapabilitiesConfig | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        del prompts
        self._providers = list(providers or ())
        self._items = {
            item.id: item
            for item in CapabilitiesManager.merge(
                default_capabilities_config(), capabilities or CapabilitiesConfig()
            ).capabilities
            if item.id != "ocr"
        }
        self._builtin_ids = {item.id for item in default_capabilities_config().capabilities}
        self._current_id = ""
        self._loading = False
        self._setup_ui()
        self._rebuild_list()

    def set_providers(self, providers: Iterable[Provider]) -> None:
        self._providers = list(providers or ())
        self.model_target_combo.set_providers(self._providers)

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)
        root.addWidget(build_page_header("能力", "为固定任务指定模型和指令。图像生成与编辑也可供对话调用；文字识别在 OCR 中配置。"))

        body = SettingsListDetailLayout()
        actions = SettingsActionBar(spacing=4)
        actions.add_icon_action("新增", Icons.get(Icons.PLUS), self._add_capability)
        self.delete_button = actions.add_icon_action(
            "删除", Icons.get(Icons.XMARK, color=Icons.COLOR_ERROR), self._delete_capability, danger=True
        )
        actions.add_stretch()
        body.list_layout.addWidget(actions)
        self.capability_list = configure_settings_resource_list(QListWidget())
        self.capability_list.currentItemChanged.connect(self._on_selected)
        body.list_layout.addWidget(self.capability_list, 1)
        body.bind(self.capability_list)

        self.editor = QWidget()
        editor_layout = QVBoxLayout(self.editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(8)
        self.editor_tabs = QTabWidget()
        editor_layout.addWidget(self.editor_tabs)

        general_page = QWidget()
        form = SettingsFormLayout(general_page, stacked_labels=True)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)

        self.id_edit = ThemedLineEdit()
        self.id_edit.setReadOnly(True)
        self.name_edit = ThemedLineEdit()
        self.enabled_check = SettingsToggle("启用")
        self.exposure_combo = QComboBox()
        self.exposure_combo.addItem("模型工具", "tool")
        self.exposure_combo.addItem("仅内部", "internal")
        configure_combo_popup(self.exposure_combo)
        self.runtime_combo = QComboBox()
        self.runtime_combo.addItem("单轮 LLM", "single_turn")
        self.runtime_combo.addItem("Agent 循环", "agent_loop")
        configure_combo_popup(self.runtime_combo)
        self.runtime_combo.currentIndexChanged.connect(self._sync_runtime_fields)
        self.operation_combo = QComboBox()
        self.operation_combo.addItem("文字与视觉理解", "text")
        self.operation_combo.addItem("图像生成与编辑", "image")
        configure_combo_popup(self.operation_combo)
        self.model_target_combo = ModelTargetCombo(self._providers, current_target=ModelTarget())
        self.description_edit = ThemedLineEdit()
        self.description_edit.setPlaceholderText("一句用途和关键约束")
        self.tool_categories_widget = QWidget()
        category_layout = QGridLayout(self.tool_categories_widget)
        category_layout.setContentsMargins(0, 0, 0, 0)
        category_layout.setHorizontalSpacing(12)
        category_layout.setVerticalSpacing(6)
        self.tool_category_checks: dict[str, QCheckBox] = {}
        for index, category in enumerate(TOOL_CATEGORIES):
            checkbox = QCheckBox(TOOL_CATEGORY_LABELS.get(category, category))
            checkbox.setToolTip(category)
            category_layout.addWidget(checkbox, index // 4, index % 4)
            self.tool_category_checks[category] = checkbox
        self.max_turns_spin = QSpinBox()
        self.max_turns_spin.setRange(1, 1000)
        self.max_turns_spin.setValue(20)

        form.addRow("标识", self.id_edit)
        form.addRow("名称", self.name_edit)
        form.addRow("状态", self.enabled_check)
        form.addRow("暴露", self.exposure_combo)
        form.addRow("用途", self.operation_combo)
        form.addRow("执行方式", self.runtime_combo)
        form.addRow("模型", self.model_target_combo)
        form.addRow("描述", self.description_edit)
        self.tool_categories_label = QLabel("工具类别")
        self.max_turns_label = QLabel("最大轮次")
        form.addRow(self.tool_categories_label, self.tool_categories_widget)
        form.addRow(self.max_turns_label, self.max_turns_spin)
        self.editor_tabs.addTab(general_page, "配置")

        prompt_page = QWidget()
        prompt_layout = QVBoxLayout(prompt_page)
        prompt_layout.setContentsMargins(8, 8, 8, 8)
        self.prompt_edit = ThemedTextEdit()
        self.prompt_edit.setAcceptRichText(False)
        self.prompt_edit.setMinimumHeight(190)
        self.prompt_edit.setPlaceholderText("Capability 系统指令")
        prompt_layout.addWidget(self.prompt_edit)
        self.editor_tabs.addTab(prompt_page, "Prompt")

        schema_page = QWidget()
        schema_form = SettingsFormLayout(schema_page, stacked_labels=True)
        schema_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        schema_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.input_schema_edit = ThemedTextEdit()
        self.input_schema_edit.setMinimumHeight(150)
        self.input_schema_edit.setPlaceholderText('{"type":"object","properties":{...}}')
        self.output_schema_edit = ThemedTextEdit()
        self.output_schema_edit.setMinimumHeight(150)
        self.output_schema_edit.setPlaceholderText('{"type":"object","required":[...]}')
        self.schema_edit = self.input_schema_edit
        schema_form.addRow("Input Schema", self.input_schema_edit)
        schema_form.addRow("Output Schema", self.output_schema_edit)
        self.editor_tabs.addTab(schema_page, "Schema")
        image_page = QWidget()
        image_layout = QVBoxLayout(image_page)
        image_section = FormSection("默认图像参数")
        self.image_size_combo = image_section.add_combo("尺寸", items=["auto", "1024x1024", "1536x1024", "1024x1536", "1K", "2K", "4K"], editable=True)
        self.image_quality_combo = image_section.add_combo("质量", items=["auto", "low", "medium", "high", "xhigh", "max"])
        self.image_format_combo = image_section.add_combo("格式", items=["auto", "png", "jpeg", "webp"])
        self.image_background_combo = image_section.add_combo("背景", items=["auto", "opaque", "transparent"])
        self.image_count_spin = image_section.add_spin("最多输出", value=1, range=(1, 4))
        note = QLabel("auto 使用服务默认。尺寸可填写宽x高；1K/2K/4K 仅用于 Seedream。Qwen / Seedream 的质量和背景保持 auto。ChatGPT 账号接口输出 PNG，暂不支持蒙版；PNG 蒙版适用于 OpenAI Images API。Seedream 组图可能少于指定数量。")
        note.setWordWrap(True)
        note.setProperty("muted", True)
        image_layout.addWidget(image_section.group)
        image_layout.addWidget(note)
        image_layout.addStretch()
        self.editor_tabs.addTab(image_page, "图像参数")
        self.operation_combo.currentIndexChanged.connect(self._sync_operation_fields)
        body.add_detail_widget(self.editor, scrollable=True)
        root.addWidget(body, 1)

    def _sync_operation_fields(self) -> None:
        image = self.operation_combo.currentData() == "image"
        self.model_target_combo.set_model_type("image" if image else "chat")
        if image:
            self.runtime_combo.setCurrentIndex(0)
        self.runtime_combo.setEnabled(not image)
        self.editor_tabs.setTabVisible(2, not image)
        self.editor_tabs.setTabVisible(3, image)
        self._sync_runtime_fields()

    def _sync_runtime_fields(self) -> None:
        agent_loop = str(self.runtime_combo.currentData() or "single_turn") == "agent_loop"
        for widget in (
            self.tool_categories_label,
            self.tool_categories_widget,
            self.max_turns_label,
            self.max_turns_spin,
        ):
            widget.setVisible(agent_loop)

    def _rebuild_list(self, selected_id: str = "") -> None:
        target = selected_id or self._current_id
        self.capability_list.blockSignals(True)
        self.capability_list.clear()
        for capability in self._items.values():
            item = SettingsStatusListItem(capability.id)
            exposure = "内部" if capability.exposure == "internal" else "工具"
            runtime = "图像" if capability.operation == "image" else "Agent" if capability.runtime == "agent_loop" else "单轮"
            item.set_status(
                capability.name,
                enabled=capability.enabled,
                detail=f"{runtime} · {exposure} · {capability.id}",
                tooltip=capability.description,
            )
            item.setData(Qt.ItemDataRole.UserRole, capability.id)
            self.capability_list.addItem(item)
        row = next(
            (index for index in range(self.capability_list.count()) if self.capability_list.item(index).data(Qt.ItemDataRole.UserRole) == target),
            0 if self.capability_list.count() else -1,
        )
        self.capability_list.setCurrentRow(row)
        self.capability_list.blockSignals(False)
        self._current_id = str(self.capability_list.currentItem().data(Qt.ItemDataRole.UserRole) or "") if row >= 0 else ""
        self._load_current()

    def _on_selected(self, current, _previous) -> None:
        if self._loading:
            return
        try:
            self._save_current()
        except ValueError as exc:
            self.capability_list.blockSignals(True)
            self.capability_list.setCurrentItem(_previous)
            self.capability_list.blockSignals(False)
            QMessageBox.warning(self, "能力配置无效", str(exc))
            return
        self._current_id = str(current.data(Qt.ItemDataRole.UserRole) or "") if current else ""
        self._load_current()

    def _load_current(self) -> None:
        capability = self._items.get(self._current_id)
        self._loading = True
        try:
            self.editor.setEnabled(capability is not None)
            self.delete_button.setEnabled(capability is not None and capability.id not in self._builtin_ids)
            if capability is None:
                return
            self.id_edit.setText(capability.id)
            self.name_edit.setText(capability.name)
            self.enabled_check.setChecked(capability.enabled)
            index = self.exposure_combo.findData(capability.exposure)
            self.exposure_combo.setCurrentIndex(index if index >= 0 else 0)
            index = self.runtime_combo.findData(capability.runtime)
            self.runtime_combo.setCurrentIndex(index if index >= 0 else 0)
            self.operation_combo.setCurrentIndex(max(0, self.operation_combo.findData(capability.operation)))
            self._sync_operation_fields()
            self.model_target_combo.set_model_target(capability.model_target)
            self.image_size_combo.setCurrentText(capability.image_options.size)
            self.image_quality_combo.setCurrentText(capability.image_options.quality)
            self.image_format_combo.setCurrentText(capability.image_options.output_format)
            self.image_background_combo.setCurrentText(capability.image_options.background)
            self.image_count_spin.setValue(capability.image_options.n)
            self.description_edit.setText(capability.description)
            self.prompt_edit.setPlainText(capability.prompt)
            selected_categories = set(capability.allowed_tool_categories)
            for category, checkbox in self.tool_category_checks.items():
                checkbox.setChecked(category in selected_categories)
            self.max_turns_spin.setValue(int(capability.max_turns or 20))
            self.input_schema_edit.setPlainText(json.dumps(capability.input_schema or {}, ensure_ascii=False, indent=2))
            self.output_schema_edit.setPlainText(json.dumps(capability.output_schema or {}, ensure_ascii=False, indent=2))
            self._sync_runtime_fields()
        finally:
            self._loading = False

    def _save_current(self) -> None:
        if self._loading or self._current_id not in self._items:
            return
        image = self.operation_combo.currentData() == "image"
        input_schema = {} if image else self._parse_schema(self.input_schema_edit, "输入")
        output_schema = {} if image else self._parse_schema(self.output_schema_edit, "输出")
        current = self._items[self._current_id]
        runtime = str(self.runtime_combo.currentData() or "single_turn")
        self._items[self._current_id] = replace(
            current,
            name=self.name_edit.text().strip() or current.id,
            enabled=self.enabled_check.isChecked(),
            exposure=str(self.exposure_combo.currentData() or "tool"),
            runtime=runtime,
            operation="image" if image else "text",
            image_options=ImageGenerationOptions(
                size=self.image_size_combo.currentText().strip(), quality=self.image_quality_combo.currentText(),
                output_format=self.image_format_combo.currentText(), background=self.image_background_combo.currentText(),
                n=self.image_count_spin.value()) if image else current.image_options,
            model_target=self.model_target_combo.model_target(),
            description=self.description_edit.text().strip(),
            prompt=self.prompt_edit.toPlainText().strip(),
            input_schema=input_schema,
            output_schema=output_schema,
            allowed_tool_categories=tuple(
                category for category, checkbox in self.tool_category_checks.items() if checkbox.isChecked()
            ) if runtime == "agent_loop" else (),
            max_turns=int(self.max_turns_spin.value()) if runtime == "agent_loop" else None,
        )

    def _parse_schema(self, editor: QTextEdit, label: str) -> dict:
        text = editor.toPlainText().strip()
        try:
            schema = json.loads(text) if text else {}
        except json.JSONDecodeError as exc:
            raise ValueError(f"{self._current_id} 的{label} Schema 不是有效 JSON：{exc.msg}") from exc
        if not isinstance(schema, dict):
            raise ValueError(f"{self._current_id} 的{label} Schema 必须是对象")
        return schema

    def _add_capability(self) -> None:
        try:
            self._save_current()
        except ValueError as exc:
            QMessageBox.warning(self, "能力配置无效", str(exc))
            return
        index = 1
        capability_id = "custom"
        while capability_id in self._items:
            index += 1
            capability_id = f"custom_{index}"
        capability_id = re.sub(r"[^a-z0-9_]+", "_", capability_id)
        self._items[capability_id] = CapabilityConfig(
            id=capability_id,
            name="自定义能力",
            exposure="tool",
            description="Run a single text transformation without tools.",
            prompt="",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
        )
        self._current_id = capability_id
        self._rebuild_list(capability_id)

    def _delete_capability(self) -> None:
        capability = self._items.get(self._current_id)
        if capability is None or capability.id in self._builtin_ids:
            return
        if QMessageBox.question(self, "删除能力", f"确定删除“{capability.name}”吗？") != QMessageBox.StandardButton.Yes:
            return
        del self._items[capability.id]
        self._current_id = ""
        self._rebuild_list()

    def collect_capabilities(self) -> CapabilitiesConfig:
        self._save_current()
        available = {option.value for option in build_model_ref_options(self._providers, model_type="image")}
        for item in self._items.values():
            if item.enabled and item.operation == "image" and item.model_target.model_ref not in available:
                raise ValueError(f"请为“{item.name}”选择可用的图像模型；先在模型与服务中添加并启用。")
        return CapabilitiesConfig(capabilities=tuple(self._items.values()))
