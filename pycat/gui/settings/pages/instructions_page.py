"""Global prompt instructions settings."""

from __future__ import annotations

from dataclasses import replace

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

from pycat.gui.settings.page_header import build_page_header
from pycat.gui.utils.form_builder import FormSection
from pycat.models.contracts.config import PromptsConfig


class InstructionsPage(QWidget):
    page_title = "指令"

    def __init__(self, prompts: PromptsConfig, parent=None, *, embedded: bool = False) -> None:
        super().__init__(parent)
        self._prompts = prompts or PromptsConfig()

        layout = QVBoxLayout(self)
        margin = 12 if embedded else 16
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.setSpacing(12)
        if not embedded:
            layout.addWidget(build_page_header(QCoreApplication.translate('InstructionsPage', '指令'), QCoreApplication.translate('InstructionsPage', '管理所有模式共享的用户级追加指令。')))

        section = FormSection(QCoreApplication.translate('InstructionsPage', '全局追加指令'))
        self.global_instructions_edit = section.add_text_edit(
            QCoreApplication.translate('InstructionsPage', '内容'),
            text=self._prompts.global_instructions,
            placeholder=QCoreApplication.translate('InstructionsPage', '追加到稳定全局原则之后；留空使用默认行为'),
            max_height=260,
            object_name="global_instructions_edit",
        )
        note = QLabel(QCoreApplication.translate('InstructionsPage', '该内容作用于所有 Mode；单个能力的行为仍在“能力 → Prompt”中配置。'))
        note.setWordWrap(True)
        note.setProperty("muted", True)
        section.form.addRow("", note)
        layout.addWidget(section.group)
        layout.addStretch(1)

    def collect(self) -> PromptsConfig:
        return replace(
            self._prompts,
            global_instructions=self.global_instructions_edit.toPlainText().strip(),
        )
