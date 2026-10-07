"""Inline projection of image regions; the canvas and LibraryService own changes."""
from PyQt6.QtCore import QSignalBlocker, QSize, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QToolButton, QVBoxLayout, QWidget

from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.theme import configure_icon_button
from pycat.gui.widgets.themed_line_edit import ThemedPlainTextEdit


class ImageAnnotationPanel(QWidget):
    selection_requested = pyqtSignal(int)
    note_changed = pyqtSignal(int, str)
    delete_requested = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 8)
        layout.setSpacing(4)
        header = QHBoxLayout()
        header.addWidget(QLabel(self.tr('图片标注')))
        header.addStretch()
        self.save, self.remove = QToolButton(), QToolButton()
        configure_icon_button(self.save, Icons.get_muted(Icons.CHECK), self.tr('保存说明'))
        configure_icon_button(self.remove, Icons.get_muted(Icons.TRASH), self.tr('删除选中标注 · Delete'))
        self.save.clicked.connect(self._save)
        self.remove.clicked.connect(lambda: self.delete_requested.emit(self.regions.currentRow()))
        header.addWidget(self.save)
        header.addWidget(self.remove)
        layout.addLayout(header)
        self.hint = QLabel(self.tr('圈选区域后添加说明；选中标注可按 Delete 删除。'))
        self.hint.setWordWrap(True)
        self.hint.setProperty('muted', True)
        layout.addWidget(self.hint)
        self.regions = QListWidget()
        self.regions.setObjectName('image_annotation_list')
        self.regions.setFrameShape(self.regions.Shape.NoFrame)
        self.regions.setIconSize(QSize(16, 16))
        self.regions.currentRowChanged.connect(self._selected)
        layout.addWidget(self.regions)
        self.note = ThemedPlainTextEdit()
        self.note.setPlaceholderText(self.tr('说明此区域需要修改或关注的内容…'))
        self.note.setAccessibleName(self.tr('选中区域的说明'))
        self.note.setFixedHeight(60)
        self.note.textChanged.connect(self._sync_save)
        layout.addWidget(self.note)
        self.set_entries([])

    def set_entries(self, entries, selected=-1):
        self._entries = [dict(entry) for entry in entries]
        with QSignalBlocker(self.regions):
            self.regions.clear()
            for number, entry in enumerate(entries, 1):
                label = self.tr('区域 {number}').format(number=number)
                note = ' '.join(entry.get('note', '').split())
                item = QListWidgetItem(label + (' · ' + note[:60] if note else ''))
                item.setToolTip(entry.get('note', ''))
                item.setSizeHint(QSize(0, 32))
                item.setIcon(Icons.get_muted(Icons.REGION))
                self.regions.addItem(item)
            self.regions.setCurrentRow(selected)
        self.regions.setVisible(bool(entries))
        self.regions.setFixedHeight(min(108, len(entries) * 32 + 4))
        self.hint.setVisible(not entries)
        self.select(selected)

    def select(self, index):
        with QSignalBlocker(self.regions):
            self.regions.setCurrentRow(index)
        valid = 0 <= index < len(self._entries)
        self.note.setVisible(valid)
        self.remove.setEnabled(valid)
        self.note.setPlainText(self._entries[index].get('note', '') if valid else '')
        self._sync_save()

    def _selected(self, index):
        self.select(index)
        self.selection_requested.emit(index)

    def _sync_save(self):
        index = self.regions.currentRow()
        self.save.setEnabled(0 <= index < len(self._entries) and self.note.toPlainText() != self._entries[index].get('note', ''))

    def _save(self):
        index = self.regions.currentRow()
        if 0 <= index < len(self._entries):
            self.note_changed.emit(index, self.note.toPlainText())
