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
    QComboBox,
    QSpinBox,
)

from core.capabilities import CapabilitiesConfig, CapabilityConfig, default_capabilities_config
from core.config.schema import PromptsConfig, PromptOptimizerConfig
from core.prompts.templates import DEFAULT_PROMPT_OPTIMIZER_SYSTEM_PROMPT
from models.provider import Provider
from ui.settings.page_header import build_page_header
from ui.widgets.model_ref_selector import ModelRefCombo


class PromptsPage(QWidget):
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
        self._capabilities = capabilities or default_capabilities_config()
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
                enabled=current.enabled,
                model_ref=model_ref,
                mode=current.mode,
                system_prompt=system_prompt,
                description=current.description,
                tool_groups=current.tool_groups,
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

        layout.addWidget(build_page_header("能力", "统一配置可复用能力，每个能力注册为 capability__* 工具供大模型直接调用。"))

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(10)

        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(6)

        self.capability_list = QListWidget()
        self.capability_list.setMinimumWidth(150)
        self.capability_list.setMaximumWidth(180)
        self.capability_list.setMinimumHeight(260)
        self.capability_list.currentRowChanged.connect(self._on_capability_changed)
        left.addWidget(self.capability_list, 1)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(6)
        self.capability_add_btn = QPushButton("新增")
        self.capability_add_btn.clicked.connect(self._add_capability)
        actions.addWidget(self.capability_add_btn)
        self.capability_delete_btn = QPushButton("删除")
        self.capability_delete_btn.clicked.connect(self._delete_capability)
        actions.addWidget(self.capability_delete_btn)
        left.addLayout(actions)

        body_layout.addLayout(left)

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

        self.capability_enabled = QCheckBox("启用此能力")
        detail_form.addRow("状态", self.capability_enabled)

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

        self.capability_mode = QComboBox()
        self.capability_mode.addItems(["agent", "chat", "plan", "explore"])
        detail_form.addRow("模式", self.capability_mode)

        self.capability_model = ModelRefCombo(
            self._providers,
            current_model_ref="",
            empty_label="跟随当前对话模型",
        )
        detail_form.addRow("模型", self.capability_model)

        self.capability_tool_groups = QLineEdit()
        self.capability_tool_groups.setPlaceholderText("例如：read, search, mcp；留空表示跟随模式")
        detail_form.addRow("工具组", self.capability_tool_groups)

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
        self.capability_builtin_btn.clicked.connect(self._load_builtin_template)
        prompt_actions.addWidget(self.capability_builtin_btn)
        self.capability_clear_prompt_btn = QPushButton("清空并使用内置")
        self.capability_clear_prompt_btn.clicked.connect(lambda: self.capability_prompt.setPlainText(""))
        prompt_actions.addWidget(self.capability_clear_prompt_btn)
        detail_form.addRow("", prompt_actions_widget)

        self.capability_options = QTextEdit()
        self.capability_options.setAcceptRichText(False)
        self.capability_options.setMaximumHeight(82)
        self.capability_options.setPlaceholderText('{"key": "value"}')
        detail_form.addRow("选项 JSON", self.capability_options)

        detail_group_layout.addLayout(detail_form)
        body_layout.addWidget(detail_group, 1)
        layout.addWidget(body, 1)

        self._populate_capabilities()

        # Compatibility aliases for older settings code/tests.
        self.prompt_opt_model_edit = self.capability_model
        self.prompt_opt_system_edit = self.capability_prompt

        hint = QLabel(
            "提示：每个能力都是一个独立的工作流，注册为 capability__* 工具。"
            "能力可配置自己的模型、工具组、最大轮次和提示词。"
            "subagent__custom 是通用子 Agent，由父 Agent 在调用时实时配置目的和工具组；"
            "subagent__read_analyze 和 subagent__search 是专用子 Agent，分别用于多文件长文分析和搜索研究。"
        )
        hint.setWordWrap(True)
        hint.setProperty("muted", True)
        layout.addWidget(hint)

        layout.addStretch()

    def _populate_capabilities(self) -> None:
        self.capability_list.clear()
        for capability in self._capability_items.values():
            enabled_mark = "✓" if capability.enabled else "–"
            item = QListWidgetItem(f"{enabled_mark} {capability.name}\n{capability.kind}")
            item.setData(Qt.ItemDataRole.UserRole, capability.id)
            item.setToolTip(f"{capability.name}\nID: {capability.id}\n类型: {capability.kind}")
            if not capability.enabled:
                item.setForeground(Qt.GlobalColor.gray)
            self.capability_list.addItem(item)
        if self.capability_list.count() > 0:
            self.capability_list.setCurrentRow(0)

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
            enabled=bool(self.capability_enabled.isChecked()),
            model_ref=self.capability_model.model_ref(),
            mode=(self.capability_mode.currentText() or current.mode or "agent").strip().lower() or "agent",
            system_prompt=(self.capability_prompt.toPlainText() or "").strip(),
            description=(self.capability_description.toPlainText() or "").strip(),
            tool_groups=self._parse_csv(self.capability_tool_groups.text()),
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
            self.capability_enabled.setChecked(bool(capability.enabled) if capability else False)
            self.capability_id.setText(capability.id if capability else "")
            self.capability_name.setText(capability.name if capability else "")
            self.capability_description.setPlainText(capability.description if capability else "")
            self._set_combo_text(self.capability_mode, capability.mode or "agent" if capability else "agent")
            self.capability_model.set_model_ref(capability.model_ref if capability else "")
            self.capability_tool_groups.setText(", ".join(capability.tool_groups or ()) if capability else "")
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
        finally:
            self._loading_capability = False

    def _capability_summary_text(self, capability: CapabilityConfig | None) -> str:
        if capability is None:
            return ""
        model = capability.model_ref or "跟随当前对话模型"
        groups = ", ".join(capability.tool_groups or ()) or "不限制 / 跟随模式"
        turns = str(capability.options.get("max_turns")) if capability.options and capability.options.get("max_turns") else "不限制"
        status = "已注册为工具" if capability.enabled else "已禁用"
        return (
            f"ID: {capability.id} · 类型: {capability.kind} · 模式: {capability.mode} · "
            f"模型: {model} · 工具组: {groups} · 最大轮次: {turns} · {status}"
        )

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
            enabled=True,
            mode="agent",
        )
        self._populate_capabilities()
        for row in range(self.capability_list.count()):
            item = self.capability_list.item(row)
            if str(item.data(Qt.ItemDataRole.UserRole) or "") == cap_id:
                self.capability_list.setCurrentRow(row)
                break

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

    @staticmethod
    def _set_combo_text(combo: QComboBox, value: str) -> None:
        target = str(value or "").strip()
        index = combo.findText(target)
        combo.setCurrentIndex(index if index >= 0 else 0)

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
