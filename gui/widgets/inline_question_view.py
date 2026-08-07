"""Inline question card for model-requested user choices."""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from gui.widgets.themed_line_edit import ThemedLineEdit

class InlineQuestionCard(QFrame):
    """Inline interactive card used by askQuestions as the primary UI path."""

    submitted = pyqtSignal(object)
    cancelled = pyqtSignal()

    def __init__(self, question: dict[str, Any], parent=None):
        super().__init__(parent)
        self.question = dict(question or {})
        self._option_controls: list[tuple[str, QWidget]] = []
        self._radio_group = QButtonGroup(self)
        self._radio_group.setExclusive(True)
        self._freeform_input: QLineEdit | None = None
        self._setup_ui()
        self._apply_recommended_defaults()

    def _setup_ui(self) -> None:
        self.setObjectName("message_widget")
        self.setProperty("role", "assistant")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)

        role_label = QLabel("助手")
        role_label.setObjectName("message_role")
        header.addWidget(role_label)

        badge = QLabel("需要选择")
        badge.setObjectName("message_badge")
        header.addWidget(badge)
        header.addStretch()
        layout.addLayout(header)

        title = str(self.question.get("id") or "需要你的选择").strip() or "需要你的选择"
        question_text = str(self.question.get("text") or "请选择一个选项").strip() or "请选择一个选项"

        title_label = QLabel(title)
        title_label.setObjectName("task_text")
        layout.addWidget(title_label)

        question_label = QLabel(question_text)
        question_label.setWordWrap(True)
        layout.addWidget(question_label)

        for index, option in enumerate(self.question.get("options") or []):
            layout.addWidget(self._build_option_widget(option, index))

        freeform_title = QLabel("补充输入")
        freeform_title.setProperty("muted", True)
        layout.addWidget(freeform_title)

        self._freeform_input = ThemedLineEdit()
        self._freeform_input.setPlaceholderText("可选：输入补充说明…")
        layout.addWidget(self._freeform_input)

        self.validation_label = QLabel("")
        self.validation_label.setObjectName("validation_error_label")
        self.validation_label.setProperty("muted", True)
        self.validation_label.setWordWrap(True)
        self.validation_label.setVisible(False)
        layout.addWidget(self.validation_label)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.cancelled.emit)
        actions.addWidget(cancel_btn)

        submit_btn = QPushButton("提交")
        submit_btn.setProperty("primary", True)
        submit_btn.clicked.connect(self._submit)
        actions.addWidget(submit_btn)
        layout.addLayout(actions)

    def _build_option_widget(self, option: Any, index: int) -> QWidget:
        payload = option if isinstance(option, dict) else {"label": str(option or "").strip()}
        label = str(payload.get("label") or "").strip() or f"选项 {index + 1}"
        description = str(payload.get("description") or "").strip()
        multi_select = bool(self.question.get("multiple", False))

        container = QFrame()
        container.setObjectName("task_card")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        if multi_select:
            control: QWidget = QCheckBox(label)
        else:
            control = QRadioButton(label)
            self._radio_group.addButton(control, index)
        layout.addWidget(control)

        if description:
            desc = QLabel(description)
            desc.setWordWrap(True)
            desc.setProperty("muted", True)
            desc_layout = QHBoxLayout()
            desc_layout.setContentsMargins(20 if not multi_select else 24, 0, 0, 0)
            desc_layout.addWidget(desc)
            layout.addLayout(desc_layout)

        self._option_controls.append((label, control))
        return container

    def _apply_recommended_defaults(self) -> None:
        options = list(self.question.get("options") or [])
        if not options:
            return

        recommended = [
            str(option.get("label") or "").strip()
            for option in options
            if isinstance(option, dict) and option.get("recommended")
        ]
        recommended = [item for item in recommended if item]
        multi_select = bool(self.question.get("multiple", False))

        if not recommended and not multi_select and self._option_controls:
            recommended = [self._option_controls[0][0]]

        for label, control in self._option_controls:
            should_select = label in recommended
            if isinstance(control, (QCheckBox, QRadioButton)):
                control.setChecked(bool(should_select))
                if should_select and not multi_select:
                    break

    def _selected_labels(self) -> list[str]:
        selected: list[str] = []
        for label, control in self._option_controls:
            if isinstance(control, (QCheckBox, QRadioButton)) and control.isChecked():
                selected.append(label)
        return selected

    def get_answer(self) -> dict[str, Any]:
        free_text = (self._freeform_input.text() if self._freeform_input else "").strip() or None
        selected = self._selected_labels()
        return {
            "selected": selected,
            "freeText": free_text,
            "skipped": not bool(selected or free_text),
        }

    def _submit(self) -> None:
        answer = self.get_answer()
        if answer["selected"] or answer["freeText"] or not self._option_controls:
            self.validation_label.setVisible(False)
            self.submitted.emit(answer)
            return

        self.validation_label.setText("请先选择至少一个选项，或输入补充说明后再提交。")
        self.validation_label.setVisible(True)
