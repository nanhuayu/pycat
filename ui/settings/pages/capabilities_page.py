from __future__ import annotations

import json
from typing import Dict, Iterable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QGroupBox,
    QFormLayout,
    QTextEdit,
    QPushButton,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QCheckBox,
    QLineEdit,
    QSpinBox,
    QComboBox,
)

from core.capabilities import CapabilitiesConfig, CapabilitiesManager, CapabilityConfig, default_capabilities_config
from core.config.schema import PromptsConfig, PromptOptimizerConfig
from core.capabilities.defaults import DEFAULT_PROMPT_OPTIMIZER_SYSTEM_PROMPT
from models.provider import Provider
from ui.settings.page_header import build_page_header
from ui.utils.combo_box import configure_combo_popup
from ui.utils.icon_manager import Icons
from ui.widgets.model_ref_selector import ModelRefCombo


class CapabilitiesPage(QWidget):
    page_title = "能力"

    def __init__(
        self,
        prompts: PromptsConfig,
        prompt_optimizer: PromptOptimizerConfig,
        providers: Iterable[Provider] | None = None,
        prompt_optimizer_model: str = "",
        capabilities: CapabilitiesConfig | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._original_prompts = prompts
        self._providers = list(providers or [])
        self._capabilities = (
            CapabilitiesManager.merge(default_capabilities_config(), capabilities)
            if capabilities is not None
            else default_capabilities_config()
        )
        self._capability_items: Dict[str, CapabilityConfig] = {
            item.id: item for item in self._capabilities.capabilities
        }
        self._builtin_capability_ids = {item.id for item in default_capabilities_config().capabilities}
        self._prompt_optimizer_selected_template = (prompt_optimizer.selected_template or "default").strip() or "default"
        self._templates: Dict[str, str] = dict(prompt_optimizer.templates or {})
        self._merge_prompt_optimizer_legacy(prompt_optimizer, prompt_optimizer_model)
        self._loading_capability = False
        self._loaded_capability_id = ""
        self._setup_ui(prompts, prompt_optimizer, prompt_optimizer_model)

    def _merge_prompt_optimizer_legacy(
        self,
        prompt_optimizer: PromptOptimizerConfig,
        prompt_optimizer_model: str,
    ) -> None:
        current = self._capability_items.get("prompt_optimize")
        if current is None:
            return

        selected = (prompt_optimizer.selected_template or "default").strip() or "default"
        legacy_prompt = str((prompt_optimizer.templates or {}).get(selected) or "").strip()
        legacy_model = str(prompt_optimizer_model or "").strip()
        default_prompt = DEFAULT_PROMPT_OPTIMIZER_SYSTEM_PROMPT.strip()
        system_prompt = current.system_prompt
        if legacy_prompt and (not system_prompt or system_prompt.strip() == default_prompt):
            system_prompt = legacy_prompt

        model_ref = current.model_ref or legacy_model
        if system_prompt != current.system_prompt or model_ref != current.model_ref:
            self._capability_items[current.id] = CapabilityConfig(
                id=current.id,
                name=current.name,
                kind=current.kind,
                visibility=current.visibility,
                execution_mode=current.execution_mode,
                model_ref=model_ref,
                system_prompt=system_prompt,
                description=current.description,
                allowed_tool_categories=current.allowed_tool_categories,
                input_schema=current.input_schema,
                output_schema=current.output_schema,
                options=current.options,
            )

    def _setup_ui(
        self,
        prompts: PromptsConfig,
        prompt_optimizer: PromptOptimizerConfig,
        prompt_optimizer_model: str,
    ) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(build_page_header("能力", "统一配置可复用大模型能力；agent_tool 能力注册为 capability__* 工具。"))

        self.capability_list = QListWidget()
        self.capability_list.setObjectName("settings_list")
        self.capability_list.setVisible(False)
        self.capability_list.currentRowChanged.connect(self._on_capability_changed)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(6)
        actions.addWidget(QLabel("能力"))
        self.capability_combo = QComboBox()
        self.capability_combo.setObjectName("settings_capability_combo")
        configure_combo_popup(self.capability_combo, popup_minimum_width=260)
        self.capability_combo.currentIndexChanged.connect(self._on_capability_combo_changed)
        actions.addWidget(self.capability_combo, 1)
        self.capability_add_btn = QPushButton("新增")
        self.capability_add_btn.setObjectName("settings_action_btn")
        self.capability_add_btn.setIcon(Icons.get(Icons.PLUS))
        self.capability_add_btn.clicked.connect(self._add_capability)
        actions.addWidget(self.capability_add_btn)
        self.capability_toggle_btn = QPushButton("停用")
        self.capability_toggle_btn.setObjectName("settings_action_btn")
        self.capability_toggle_btn.setIcon(Icons.get(Icons.PAUSE, scale_factor=1.0))
        self.capability_toggle_btn.clicked.connect(self._toggle_capability_enabled)
        actions.addWidget(self.capability_toggle_btn)
        self.capability_delete_btn = QPushButton("删除")
        self.capability_delete_btn.setObjectName("settings_action_btn")
        self.capability_delete_btn.setProperty("danger", True)
        self.capability_delete_btn.setIcon(Icons.get(Icons.XMARK, color=Icons.COLOR_ERROR))
        self.capability_delete_btn.clicked.connect(self._delete_capability)
        actions.addWidget(self.capability_delete_btn)
        layout.addLayout(actions)

        detail_group = QGroupBox("能力详情")
        detail_group.setProperty("flat", True)
        detail_group_layout = QVBoxLayout(detail_group)
        detail_group_layout.setContentsMargins(10, 8, 10, 10)
        detail_group_layout.setSpacing(6)

        self.capability_summary = QLabel("")
        self.capability_summary.setWordWrap(True)
        self.capability_summary.setProperty("muted", True)
        detail_group_layout.addWidget(self.capability_summary)

        detail_form = QFormLayout()
        detail_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        detail_form.setHorizontalSpacing(10)
        detail_form.setVerticalSpacing(6)

        self.capability_visibility = QComboBox()
        self.capability_visibility.addItem("内部调用", "internal")
        self.capability_visibility.addItem("注册为工具", "agent_tool")
        self.capability_visibility.addItem("停用", "hidden")
        configure_combo_popup(self.capability_visibility)
        detail_form.addRow("可见性", self.capability_visibility)

        self.capability_id = QLineEdit()
        self.capability_id.setReadOnly(True)
        detail_form.addRow("ID", self.capability_id)

        self.capability_name = QLineEdit()
        detail_form.addRow("名称", self.capability_name)

        self.capability_description = QTextEdit()
        self.capability_description.setAcceptRichText(False)
        self.capability_description.setMaximumHeight(64)
        self.capability_description.setPlaceholderText("简短说明此能力的用途")
        detail_form.addRow("说明", self.capability_description)

        self.capability_model = ModelRefCombo(
            self._providers,
            current_model_ref="",
            empty_label="跟随当前对话模型",
        )
        detail_form.addRow("模型", self.capability_model)

        self.capability_tool_categories = QLineEdit()
        self.capability_tool_categories.setPlaceholderText("例如：read, search, mcp；留空表示文本能力")
        detail_form.addRow("允许工具类别", self.capability_tool_categories)

        self.capability_max_turns = QSpinBox()
        self.capability_max_turns.setRange(0, 100)
        self.capability_max_turns.setSpecialValueText("不限制")
        detail_form.addRow("最大轮次", self.capability_max_turns)

        self.capability_prompt = QTextEdit()
        self.capability_prompt.setAcceptRichText(False)
        self.capability_prompt.setMaximumHeight(108)
        self.capability_prompt.setPlaceholderText("留空则使用能力内置提示词")
        detail_form.addRow("提示词", self.capability_prompt)

        prompt_actions_widget = QWidget()
        prompt_actions = QHBoxLayout(prompt_actions_widget)
        prompt_actions.setContentsMargins(0, 0, 0, 0)
        prompt_actions.setSpacing(6)
        prompt_actions.addStretch()
        self.capability_builtin_btn = QPushButton("恢复内置提示词")
        self.capability_builtin_btn.setObjectName("settings_action_btn")
        self.capability_builtin_btn.setIcon(Icons.get(Icons.REFRESH))
        self.capability_builtin_btn.clicked.connect(self._load_builtin_template)
        prompt_actions.addWidget(self.capability_builtin_btn)
        self.capability_clear_prompt_btn = QPushButton("清空并使用内置")
        self.capability_clear_prompt_btn.setObjectName("settings_action_btn")
        self.capability_clear_prompt_btn.setIcon(Icons.get(Icons.XMARK, color=Icons.COLOR_ERROR))
        self.capability_clear_prompt_btn.clicked.connect(lambda: self.capability_prompt.setPlainText(""))
        prompt_actions.addWidget(self.capability_clear_prompt_btn)
        detail_form.addRow("", prompt_actions_widget)

        self.capability_options = QTextEdit()
        self.capability_options.setAcceptRichText(False)
        self.capability_options.setMaximumHeight(82)
        self.capability_options.setPlaceholderText('{"key": "value"}')
        detail_form.addRow("选项 JSON", self.capability_options)

        detail_group_layout.addLayout(detail_form)
        layout.addWidget(detail_group, 1)

        self._populate_capabilities()

        # Compatibility aliases for older settings code/tests.
        self.prompt_opt_model_edit = self.capability_model
        self.prompt_opt_system_edit = self.capability_prompt

        hint = QLabel(
            "提示：internal 能力仅供运行时和界面内部调用；agent_tool 能力会注册为 capability__* 工具；"
            "hidden 能力不可调用。能力可配置自己的模型、允许工具类别、最大轮次和提示词。"
        )
        hint.setWordWrap(True)
        hint.setProperty("muted", True)
        layout.addWidget(hint)

        layout.addStretch()

    def _populate_capabilities(self, *, select_id: str = "") -> None:
        current_id = str(select_id or self._current_capability_id() or "").strip()
        self.capability_list.blockSignals(True)
        if hasattr(self, "capability_combo"):
            self.capability_combo.blockSignals(True)
            self.capability_combo.clear()
        self.capability_list.clear()
        try:
            for capability in self._capability_items.values():
                visibility_mark = {"agent_tool": "工具", "internal": "内部", "hidden": "停用"}.get(capability.visibility, "?")
                item = QListWidgetItem(f"{capability.name}\n{visibility_mark} · {capability.kind}")
                item.setData(Qt.ItemDataRole.UserRole, capability.id)
                item.setToolTip(f"{capability.name}\nID: {capability.id}\n类型: {capability.kind}\n状态: {visibility_mark}")
                if capability.visibility == "hidden":
                    item.setForeground(Qt.GlobalColor.gray)
                self.capability_list.addItem(item)
                if hasattr(self, "capability_combo"):
                    self.capability_combo.addItem(f"{capability.name} ({visibility_mark})", capability.id)
            if self.capability_list.count() > 0:
                target_row = 0
                if current_id:
                    for row in range(self.capability_list.count()):
                        item = self.capability_list.item(row)
                        if str(item.data(Qt.ItemDataRole.UserRole) or "") == current_id:
                            target_row = row
                            break
                self.capability_list.setCurrentRow(target_row)
                if hasattr(self, "capability_combo"):
                    self.capability_combo.setCurrentIndex(target_row)
        finally:
            self.capability_list.blockSignals(False)
            if hasattr(self, "capability_combo"):
                self.capability_combo.blockSignals(False)
        self._on_capability_changed(self.capability_list.currentRow())

    def _on_capability_combo_changed(self, row: int) -> None:
        if 0 <= int(row) < self.capability_list.count():
            self.capability_list.setCurrentRow(int(row))

    def _current_capability_id(self) -> str:
        item = self.capability_list.currentItem()
        if item is None:
            return ""
        return str(item.data(Qt.ItemDataRole.UserRole) or "").strip()

    def _save_capability(self, capability_id: str) -> None:
        capability_id = str(capability_id or "").strip()
        if not capability_id:
            return
        current = self._capability_items.get(capability_id)
        if current is None:
            return

        options = dict(current.options or {})
        options_text = (self.capability_options.toPlainText() or "").strip()
        if options_text:
            try:
                parsed = json.loads(options_text)
                if isinstance(parsed, dict):
                    options = parsed
                    self.capability_options.setToolTip("")
                else:
                    self.capability_options.setToolTip("选项 JSON 必须是对象；已保留上一次有效配置。")
            except Exception as exc:
                self.capability_options.setToolTip(f"选项 JSON 无效，已保留上一次有效配置：{exc}")
        else:
            options = {}

        max_turns = int(self.capability_max_turns.value())
        if max_turns > 0:
            options["max_turns"] = max_turns
        else:
            options.pop("max_turns", None)

        self._capability_items[capability_id] = CapabilityConfig(
            id=current.id,
            name=(self.capability_name.text() or "").strip() or current.name or current.id,
            kind=current.kind,
            visibility=str(self.capability_visibility.currentData() or current.visibility or "agent_tool"),
            execution_mode="tool_limited_loop" if self._parse_csv(self.capability_tool_categories.text()) else "direct_llm",
            model_ref=self.capability_model.model_ref(),
            system_prompt=(self.capability_prompt.toPlainText() or "").strip(),
            description=(self.capability_description.toPlainText() or "").strip(),
            allowed_tool_categories=self._parse_csv(self.capability_tool_categories.text()),
            input_schema=current.input_schema,
            output_schema=current.output_schema,
            options=options,
        )

    def _save_current_capability(self) -> None:
        self._save_capability(self._current_capability_id())

    def _on_capability_changed(self, _row: int) -> None:
        previous = getattr(self, "_loading_capability", False)
        if not previous:
            old_id = getattr(self, "_loaded_capability_id", "")
            if old_id:
                self._save_capability(old_id)
        capability_id = self._current_capability_id()
        capability = self._capability_items.get(capability_id)
        self._loading_capability = True
        try:
            self._loaded_capability_id = capability_id
            index = self.capability_visibility.findData(capability.visibility if capability else "hidden")
            self.capability_visibility.setCurrentIndex(index if index >= 0 else 0)
            self.capability_id.setText(capability.id if capability else "")
            self.capability_name.setText(capability.name if capability else "")
            self.capability_description.setPlainText(capability.description if capability else "")
            self.capability_model.set_model_ref(capability.model_ref if capability else "")
            self.capability_tool_categories.setText(", ".join(capability.allowed_tool_categories or ()) if capability else "")
            max_turns = 0
            if capability and capability.options:
                try:
                    raw = capability.options.get("max_turns")
                    max_turns = int(raw) if raw not in (None, "") else 0
                except Exception:
                    max_turns = 0
            self.capability_max_turns.setValue(max_turns if max_turns > 0 else 0)
            self.capability_prompt.setPlainText(capability.system_prompt if capability else "")
            options = dict(capability.options or {}) if capability else {}
            self.capability_options.setPlainText(
                json.dumps(options, ensure_ascii=False, indent=2) if options else ""
            )
            self.capability_summary.setText(self._capability_summary_text(capability))
            self.capability_delete_btn.setEnabled(
                (capability_id not in self._builtin_capability_ids) if capability else False
            )
            if hasattr(self, "capability_combo"):
                row = self.capability_list.currentRow()
                if row >= 0 and self.capability_combo.currentIndex() != row:
                    self.capability_combo.blockSignals(True)
                    self.capability_combo.setCurrentIndex(row)
                    self.capability_combo.blockSignals(False)
            self._sync_toggle_action(capability)
        finally:
            self._loading_capability = False

    def _capability_summary_text(self, capability: CapabilityConfig | None) -> str:
        if capability is None:
            return ""
        model = capability.model_ref or "跟随当前对话模型"
        categories = ", ".join(capability.allowed_tool_categories or ()) or "无（文本能力）"
        runtime = capability.execution_mode
        turns = str(capability.options.get("max_turns")) if capability.options and capability.options.get("max_turns") else "不限制"
        status = {"agent_tool": "注册为工具", "internal": "内部调用", "hidden": "停用"}.get(
            capability.visibility,
            capability.visibility,
        )
        return (
            f"ID: {capability.id} · 类型: {capability.kind} · 运行: {runtime} · "
            f"模型: {model} · 允许工具类别: {categories} · 最大轮次: {turns} · {status}"
        )

    def _sync_toggle_action(self, capability: CapabilityConfig | None) -> None:
        if not hasattr(self, "capability_toggle_btn"):
            return
        enabled = capability is not None and capability.visibility != "hidden"
        self.capability_toggle_btn.setEnabled(capability is not None)
        self.capability_toggle_btn.setText("停用" if enabled else "启用")
        self.capability_toggle_btn.setIcon(Icons.get(Icons.PAUSE if enabled else Icons.PLAY, scale_factor=1.0))

    def _toggle_capability_enabled(self) -> None:
        capability_id = self._current_capability_id()
        if not capability_id:
            return
        self._save_capability(capability_id)
        current = self._capability_items.get(capability_id)
        if current is None:
            return
        if current.visibility == "hidden":
            builtin = default_capabilities_config().capability(capability_id)
            new_visibility = str(getattr(builtin, "visibility", "") or "").strip() or "agent_tool"
            if new_visibility == "hidden":
                new_visibility = "agent_tool"
        else:
            new_visibility = "hidden"
        self._capability_items[capability_id] = CapabilityConfig(
            id=current.id,
            name=current.name,
            kind=current.kind,
            visibility=new_visibility,
            execution_mode=current.execution_mode,
            model_ref=current.model_ref,
            system_prompt=current.system_prompt,
            description=current.description,
            allowed_tool_categories=current.allowed_tool_categories,
            input_schema=current.input_schema,
            output_schema=current.output_schema,
            options=current.options,
        )
        self._loaded_capability_id = ""
        self._populate_capabilities(select_id=capability_id)

    def _add_capability(self) -> None:
        self._save_capability(getattr(self, "_loaded_capability_id", "") or self._current_capability_id())
        base = "custom_capability"
        index = 1
        while f"{base}_{index}" in self._capability_items:
            index += 1
        cap_id = f"{base}_{index}"
        self._capability_items[cap_id] = CapabilityConfig(
            id=cap_id,
            name=f"自定义能力 {index}",
            kind="custom",
            visibility="agent_tool",
            execution_mode="direct_llm",
        )
        self._loaded_capability_id = ""
        self._populate_capabilities(select_id=cap_id)

    def _delete_capability(self) -> None:
        capability_id = self._current_capability_id()
        if not capability_id:
            return
        if capability_id in self._builtin_capability_ids:
            self.capability_delete_btn.setToolTip("内置能力不能删除，可通过编辑内容调整。")
            return
        row = self.capability_list.currentRow()
        self._capability_items.pop(capability_id, None)
        self._populate_capabilities()
        if self.capability_list.count() > 0:
            self.capability_list.setCurrentRow(min(max(row, 0), self.capability_list.count() - 1))

    @staticmethod
    def _parse_csv(text: str) -> tuple[str, ...]:
        values: list[str] = []
        for raw in str(text or "").replace("，", ",").split(","):
            value = raw.strip()
            if value and value not in values:
                values.append(value)
        return tuple(values)

    def _load_builtin_template(self) -> None:
        capability_id = self._current_capability_id()
        builtin = default_capabilities_config().capability(capability_id)
        prompt = str(getattr(builtin, "system_prompt", "") or "").strip()
        if not prompt and capability_id == "prompt_optimize":
            prompt = DEFAULT_PROMPT_OPTIMIZER_SYSTEM_PROMPT.strip()
        self.capability_prompt.setPlainText(prompt)

    def collect_prompts(self) -> PromptsConfig:
        return self._original_prompts

    def collect_prompt_optimizer(self) -> PromptOptimizerConfig:
        self._save_capability(getattr(self, "_loaded_capability_id", "") or self._current_capability_id())
        sel = self._prompt_optimizer_selected_template or "default"
        prompt_capability = self._capability_items.get("prompt_optimize")
        content = str(getattr(prompt_capability, "system_prompt", "") or "").strip()
        templates = dict(self._templates or {})
        templates[sel] = content
        return PromptOptimizerConfig(selected_template=sel, templates=templates)

    def collect_prompt_optimizer_model(self) -> str:
        self._save_capability(getattr(self, "_loaded_capability_id", "") or self._current_capability_id())
        prompt_capability = self._capability_items.get("prompt_optimize")
        return str(getattr(prompt_capability, "model_ref", "") or "").strip()

    def collect_capabilities(self) -> CapabilitiesConfig:
        self._save_capability(getattr(self, "_loaded_capability_id", "") or self._current_capability_id())
        return CapabilitiesConfig(capabilities=tuple(self._capability_items.values()))
