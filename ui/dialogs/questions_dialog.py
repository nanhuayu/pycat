from __future__ import annotations

from typing import Any

from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)


class QuestionsDialog(QDialog):
    """Interactive dialog for a single askQuestions prompt."""

    def __init__(self, question: dict[str, Any], parent=None):
        super().__init__(parent)
        self._question = dict(question or {})
        self._option_controls: list[tuple[str, QWidget]] = []
        self._radio_group = QButtonGroup(self)
        self._radio_group.setExclusive(True)
        self._freeform_input: QLineEdit | None = None
        self._setup_ui()
        self._apply_recommended_defaults()

    def _setup_ui(self) -> None:
        header = str(self._question.get("header") or "需要你的选择").strip() or "需要你的选择"
        question_text = str(self._question.get("question") or "请选择一个选项").strip() or "请选择一个选项"
        message_text = str(self._question.get("message") or "").strip()

        self.setWindowTitle(header)
        self.setModal(True)
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        if header and header != question_text:
            header_label = QLabel(header)
            header_label.setObjectName("dialog_title")
            header_label.setWordWrap(True)
            layout.addWidget(header_label)

        question_label = QLabel(question_text)
        question_label.setWordWrap(True)
        layout.addWidget(question_label)

        if message_text:
            message_label = QLabel(message_text)
            message_label.setWordWrap(True)
            message_label.setProperty("muted", True)
            layout.addWidget(message_label)

        options = list(self._question.get("options") or [])
        if options:
            options_container = QFrame()
            options_layout = QVBoxLayout(options_container)
            options_layout.setContentsMargins(0, 0, 0, 0)
            options_layout.setSpacing(8)
            for index, option in enumerate(options):
                option_widget = self._build_option_widget(option, index)
                options_layout.addWidget(option_widget)
            layout.addWidget(options_container)

        if bool(self._question.get("allow_freeform_input", True)):
            freeform_label = QLabel("补充输入")
            freeform_label.setProperty("muted", True)
            layout.addWidget(freeform_label)

            self._freeform_input = QLineEdit()
            self._freeform_input.setPlaceholderText("可选：直接输入你的回答…")
            layout.addWidget(self._freeform_input)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _build_option_widget(self, option: Any, index: int) -> QWidget:
        payload = option if isinstance(option, dict) else {"label": str(option or "").strip()}
        label = str(payload.get("label") or "").strip() or f"选项 {index + 1}"
        description = str(payload.get("description") or "").strip()
        multi_select = bool(self._question.get("multi_select", False))

        container = QFrame()
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
            desc_label = QLabel(description)
            desc_label.setWordWrap(True)
            desc_label.setProperty("muted", True)
            desc_layout = QHBoxLayout()
            desc_layout.setContentsMargins(20 if not multi_select else 24, 0, 0, 0)
            desc_layout.addWidget(desc_label)
            layout.addLayout(desc_layout)

        self._option_controls.append((label, control))
        return container

    def _apply_recommended_defaults(self) -> None:
        options = list(self._question.get("options") or [])
        if not options or not self._option_controls:
            return

        multi_select = bool(self._question.get("multi_select", False))
        recommended_labels = [
            str(option.get("label") or "").strip()
            for option in options
            if isinstance(option, dict) and option.get("recommended")
        ]
        recommended_labels = [label for label in recommended_labels if label]

        if not recommended_labels and not multi_select:
            recommended_labels = [self._option_controls[0][0]]

        for label, control in self._option_controls:
            should_select = label in recommended_labels
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

    def _accept_if_valid(self) -> None:
        selected = self._selected_labels()
        free_text = (self._freeform_input.text() if self._freeform_input else "").strip()

        if selected or free_text:
            self.accept()
            return

        if self._option_controls:
            QMessageBox.information(self, "提示", "请先选择至少一个选项，或输入补充回答。")
            return

        if self._freeform_input is not None:
            QMessageBox.information(self, "提示", "请输入回答后再继续。")
            return

        self.reject()

    def get_answer(self) -> dict[str, Any]:
        free_text = (self._freeform_input.text() if self._freeform_input else "").strip() or None
        selected = self._selected_labels()
        skipped = not bool(selected or free_text)
        return {
            "selected": selected,
            "freeText": free_text,
            "skipped": skipped,
        }
