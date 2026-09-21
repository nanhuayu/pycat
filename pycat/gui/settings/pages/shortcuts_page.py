"""Editable application shortcuts inside the shared settings save lifecycle."""
from PyQt6.QtGui import QKeySequence
from PyQt6.QtWidgets import QHBoxLayout, QKeySequenceEdit, QLabel, QPushButton, QVBoxLayout, QWidget

from pycat.gui.shortcuts import SHORTCUTS, shortcut_sequence, validate_shortcuts
from pycat.gui.widgets.themed_line_edit import ThemedLineEdit


class ShortcutsPage(QWidget):
    page_title = '快捷键'

    def __init__(self, overrides=None, parent=None):
        super().__init__(parent)
        self.edits, self.rows = {}, {}
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        header = QHBoxLayout()
        self.search = ThemedLineEdit()
        self.search.setPlaceholderText('查找操作或快捷键')
        header.addWidget(self.search, 1)
        self.reset_button = QPushButton('恢复默认')
        self.reset_button.clicked.connect(self.reset)
        header.addWidget(self.reset_button)
        root.addLayout(header)
        hint = QLabel('点击按键框重新绑定，清除可停用，保存后生效。Windows 的截图快捷键全局有效；其他操作仅在 PyCat 内有效。')
        hint.setWordWrap(True)
        hint.setProperty('muted', True)
        root.addWidget(hint)
        self.capture_status = QLabel()
        self.capture_status.setWordWrap(True)
        self.capture_status.setProperty('muted', True)
        root.addWidget(self.capture_status)
        for spec in SHORTCUTS:
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 2, 0, 2)
            layout.addWidget(QLabel(spec.label), 1)
            edit = QKeySequenceEdit(QKeySequence(shortcut_sequence(spec.id, overrides)))
            edit.setMaximumSequenceLength(1)
            edit.setClearButtonEnabled(spec.editable)
            edit.setEnabled(spec.editable)
            edit.setFixedWidth(190)
            edit.setAccessibleName(spec.label)
            if not spec.editable:
                edit.setToolTip('Esc 保留为统一取消键')
            self.edits[spec.id], self.rows[spec.id] = edit, row
            layout.addWidget(edit)
            root.addWidget(row)
        root.addStretch()
        self.search.textChanged.connect(self._filter)

    def reset(self):
        for spec in SHORTCUTS:
            self.edits[spec.id].setKeySequence(QKeySequence(spec.sequence))

    def collect(self):
        return validate_shortcuts({key: edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText) for key, edit in self.edits.items()})

    def _filter(self, text):
        query = text.casefold().strip()
        for spec in SHORTCUTS:
            words = spec.label + self.edits[spec.id].keySequence().toString()
            self.rows[spec.id].setVisible(not query or query in words.casefold())
