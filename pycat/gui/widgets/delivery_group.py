"""Compact output projection with lazy rows and bounded image strips."""
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget


class DeliveryGroup(QFrame):
    INITIAL_COUNT = 3
    BATCH_SIZE = 12

    def __init__(self, refs, create_widget, parent=None):
        super().__init__(parent)
        self.setObjectName('delivery_group')
        self.refs = tuple(refs)
        self._create_widget = create_widget
        self.visible_count = 0
        self._image_row = None
        self._image_count = 0
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)
        title = QLabel(QCoreApplication.translate('DeliveryGroup', '本次产出 · {value}').format(value=len(self.refs)))
        title.setObjectName('delivery_section_title')
        title.setProperty('muted', True)
        root.addWidget(title)
        self.body = QWidget()
        self.rows = QVBoxLayout(self.body)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(4)
        root.addWidget(self.body)
        actions = QHBoxLayout()
        self.more_button = QPushButton()
        self.more_button.setFlat(True)
        self.more_button.clicked.connect(lambda: self._append(self.BATCH_SIZE))
        actions.addWidget(self.more_button)
        actions.addStretch()
        self.collapse_button = QPushButton(QCoreApplication.translate('DeliveryGroup', '收起'))
        self.collapse_button.setFlat(True)
        self.collapse_button.clicked.connect(self.collapse)
        actions.addWidget(self.collapse_button)
        root.addLayout(actions)
        self._append(self.INITIAL_COUNT)

    def _append(self, count):
        for ref in self.refs[self.visible_count:self.visible_count + count]:
            widget = self._create_widget(ref)
            if widget.target is not None and widget.target.is_image:
                if self._image_row is None or self._image_count == 3:
                    row = QHBoxLayout()
                    row.setContentsMargins(0, 0, 0, 0)
                    row.setSpacing(8)
                    row.addStretch()
                    self.rows.addLayout(row)
                    self._image_row, self._image_count = row, 0
                self._image_row.insertWidget(self._image_count, widget)
                self._image_count += 1
            else:
                self._image_row, self._image_count = None, 0
                self.rows.addWidget(widget)
            self.visible_count += 1
        remaining = len(self.refs) - self.visible_count
        self.more_button.setText(QCoreApplication.translate('DeliveryGroup', '再显示 {value} 项').format(value=min(remaining, self.BATCH_SIZE)) + (QCoreApplication.translate('DeliveryGroup', '（剩余 {remaining}）').format(remaining=remaining) if remaining > self.BATCH_SIZE else ''))
        self.more_button.setVisible(remaining > 0)
        self.collapse_button.setVisible(self.visible_count > self.INITIAL_COUNT)

    def collapse(self):
        old = self.body
        self.body = QWidget()
        self.rows = QVBoxLayout(self.body)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(4)
        self.layout().replaceWidget(old, self.body)
        old.hide()
        old.setParent(None)
        old.deleteLater()
        self.visible_count = 0
        self._image_row, self._image_count = None, 0
        self._append(self.INITIAL_COUNT)
