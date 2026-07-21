"""Settings editor for single-call LLM capabilities."""
from __future__ import annotations

from dataclasses import replace
import json
import re
from typing import Iterable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.capabilities import CapabilitiesConfig, CapabilitiesManager, CapabilityConfig, default_capabilities_config
from gui.settings.components import SettingsActionBar, SettingsListDetailLayout, SettingsStatusListItem, configure_settings_resource_list
from gui.settings.page_header import build_page_header
from gui.utils.combo_box import configure_combo_popup
from gui.utils.icon_manager import Icons
from gui.widgets.model_ref_selector import ModelTargetCombo
from models.contracts.config import PromptsConfig
from models.contracts.model_target import ModelTarget
from models.contracts.tooling import TOOL_CATEGORIES, TOOL_CATEGORY_LABELS
from models.provider import Provider


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
        root.addWidget(build_page_header("能力", "能力可执行单轮转换或受限的 Agent 循环；工作流仍由独立编排层负责。"))

        body = SettingsListDetailLayout("能力", "配置", list_stretch=2, detail_stretch=5)
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

        self.editor = QWidget()
        editor_layout = QVBoxLayout(self.editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(8)
        self.editor_tabs = QTabWidget()
        editor_layout.addWidget(self.editor_tabs)

        general_page = QWidget()
        form = QFormLayout(general_page)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)

        self.id_edit = QLineEdit()
        self.id_edit.setReadOnly(True)
        self.name_edit = QLineEdit()
        self.enabled_check = QCheckBox("启用")
        self.exposure_combo = QComboBox()
        self.exposure_combo.addItem("模型工具", "tool")
        self.exposure_combo.addItem("仅内部", "internal")
        configure_combo_popup(self.exposure_combo)
        self.runtime_combo = QComboBox()
        self.runtime_combo.addItem("单轮 LLM", "single_turn")
        self.runtime_combo.addItem("Agent 循环", "agent_loop")
        configure_combo_popup(self.runtime_combo)
        self.runtime_combo.currentIndexChanged.connect(self._sync_runtime_fields)
        self.model_target_combo = ModelTargetCombo(self._providers, current_target=ModelTarget())
        self.description_edit = QLineEdit()
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
        form.addRow("Runtime", self.runtime_combo)
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
        self.prompt_edit = QTextEdit()
        self.prompt_edit.setAcceptRichText(False)
        self.prompt_edit.setMinimumHeight(190)
        self.prompt_edit.setPlaceholderText("Capability 系统指令")
        prompt_layout.addWidget(self.prompt_edit)
        self.editor_tabs.addTab(prompt_page, "Prompt")

        schema_page = QWidget()
        schema_form = QFormLayout(schema_page)
        schema_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.input_schema_edit = QTextEdit()
        self.input_schema_edit.setMinimumHeight(150)
        self.input_schema_edit.setPlaceholderText('{"type":"object","properties":{...}}')
        self.output_schema_edit = QTextEdit()
        self.output_schema_edit.setMinimumHeight(150)
        self.output_schema_edit.setPlaceholderText('{"type":"object","required":[...]}')
        self.schema_edit = self.input_schema_edit
        schema_form.addRow("Input Schema", self.input_schema_edit)
        schema_form.addRow("Output Schema", self.output_schema_edit)
        self.editor_tabs.addTab(schema_page, "Schema")
        body.add_detail_widget(self.editor, scrollable=True)
        root.addWidget(body, 1)

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
            runtime = "Agent" if capability.runtime == "agent_loop" else "单轮"
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
        self._save_current()
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
            self.model_target_combo.set_model_target(capability.model_target)
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
        input_schema = self._parse_schema(self.input_schema_edit, "输入")
        output_schema = self._parse_schema(self.output_schema_edit, "输出")
        current = self._items[self._current_id]
        runtime = str(self.runtime_combo.currentData() or "single_turn")
        self._items[self._current_id] = replace(
            current,
            name=self.name_edit.text().strip() or current.id,
            enabled=self.enabled_check.isChecked(),
            exposure=str(self.exposure_combo.currentData() or "tool"),
            runtime=runtime,
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
        self._save_current()
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
        return CapabilitiesConfig(capabilities=tuple(self._items.values()))
