"""Shared instruction draft and navigation to project rules and materials."""
from __future__ import annotations

from dataclasses import replace

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from pycat.gui.settings.page_header import build_page_header
from pycat.gui.utils.form_builder import FormSection
from pycat.gui.widgets.themed_line_edit import ThemedTextEdit
from pycat.models.contracts.config import PromptsConfig


class MemoryPage(QWidget):
    page_title = "记忆与资料"
    library_requested = pyqtSignal()
    project_instructions_requested = pyqtSignal()

    def __init__(self, prompts: PromptsConfig, *, work_dir: str = "", parent=None) -> None:
        super().__init__(parent)
        self._prompts = prompts
        layout = QVBoxLayout(self)
        layout.addWidget(build_page_header(self.tr("记忆与资料"),
            self.tr("设置全局追加指令，查看项目规则、记忆和资料。")))

        instructions = FormSection(self.tr("全局追加指令"))
        self.global_instructions_edit = ThemedTextEdit()
        self.global_instructions_edit.setObjectName("global_instructions_edit")
        self.global_instructions_edit.setAccessibleName(self.tr("全局追加指令"))
        self.global_instructions_edit.setAcceptRichText(False)
        self.global_instructions_edit.setPlainText(prompts.global_instructions)
        self.global_instructions_edit.setPlaceholderText(self.tr("应用于所有模式；留空使用默认行为。"))
        self.global_instructions_edit.setMinimumHeight(120)
        self.global_instructions_edit.setMaximumHeight(180)
        instructions.form.addRow(self.global_instructions_edit)
        layout.addWidget(instructions.group)

        context = FormSection(self.tr("环境上下文"))
        self.include_environment_check = context.add_checkbox(
            self.tr("包含环境信息"), checked=prompts.include_environment)
        self.file_tree_max_depth_spin = context.add_spin(
            self.tr("项目树深度"), value=prompts.file_tree_max_depth,
            range=(1, max(10, prompts.file_tree_max_depth)))
        layout.addWidget(context.group)

        materials = FormSection(self.tr("项目与资料"))
        self.project_instructions_button = QPushButton(self.tr("查看"))
        self.project_instructions_button.setAccessibleName(self.tr("查看项目 AGENTS.md"))
        self.project_instructions_button.setEnabled(bool(work_dir))
        self.project_instructions_button.setToolTip(work_dir or self.tr("当前未选择项目"))
        self.project_instructions_button.clicked.connect(self.project_instructions_requested)
        materials.form.addRow(QLabel(self.tr("项目指令") + "\nAGENTS.md"), self.project_instructions_button)
        self.library_button = QPushButton(self.tr("打开"))
        self.library_button.setAccessibleName(self.tr("打开记忆与资料"))
        self.library_button.clicked.connect(self.library_requested)
        materials.form.addRow(QLabel(self.tr("记忆与资料").replace("&", "&&") + "\n" +
            self.tr("阅读文件、审核项目记忆与用户偏好。")), self.library_button)
        layout.addWidget(materials.group)
        layout.addStretch()

    def collect(self) -> PromptsConfig:
        """Project the edited prompt fields through the existing settings save."""
        return replace(self._prompts,
            global_instructions=self.global_instructions_edit.toPlainText().strip(),
            include_environment=self.include_environment_check.isChecked(),
            file_tree_max_depth=self.file_tree_max_depth_spin.value())
