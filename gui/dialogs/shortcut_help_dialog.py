"""Compact read-only keyboard shortcut reference."""
from __future__ import annotations

from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from gui.shortcuts import SHORTCUTS


class ShortcutHelpDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("快捷键")
        self.setObjectName("shortcut_help_dialog")
        self.setMinimumSize(440, 340)
        self.resize(480, 380)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(8)

        title = QLabel("快捷键")
        title.setObjectName("settings_page_title")
        root.addWidget(title)
        description = QLabel("常用操作可以直接从键盘完成。")
        description.setProperty("muted", True)
        root.addWidget(description)

        current_group = ""
        for spec in SHORTCUTS:
            if spec.group != current_group:
                current_group = spec.group
                group_label = QLabel(current_group)
                group_label.setObjectName("shortcut_group_label")
                root.addWidget(group_label)

            row = QWidget()
            row.setObjectName("shortcut_row")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(8, 3, 8, 3)
            row_layout.setSpacing(8)
            row_layout.addWidget(QLabel(spec.label), 1)
            key = QLabel(spec.sequence)
            key.setObjectName("shortcut_key")
            row_layout.addWidget(key)
            root.addWidget(row)

        root.addStretch(1)
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("settings_action_btn")
        close_btn.clicked.connect(self.accept)
        footer = QHBoxLayout()
        footer.addStretch(1)
        footer.addWidget(close_btn)
        root.addLayout(footer)
