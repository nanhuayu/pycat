"""Attachment preview components extracted from input_area.py."""
from __future__ import annotations

import os

from PyQt6.QtCore import QCoreApplication, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from pycat.gui.utils.image_loader import load_pixmap


class AttachmentPreviewItem(QFrame):
    """Preview item for attached images or files."""

    remove_requested = pyqtSignal(str)
    WIDTH, HEIGHT = 80, 72

    def __init__(
        self,
        source: str,
        is_image: bool = True,
        *,
        error: str = "",
        display_name: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.source = source
        self.is_image = is_image
        self.display_name = str(display_name or "").strip()
        self._setup_ui()
        self.set_error(error)

    def _setup_ui(self) -> None:
        self.setObjectName("image_preview_item")
        self.setFixedSize(self.WIDTH, self.HEIGHT)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(1)

        thumb = QLabel()
        self._thumb = thumb
        thumb.setObjectName("image_thumb")
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)

        if self.is_image:
            pixmap = load_pixmap(self.source)
            if not pixmap.isNull():
                scaled = pixmap.scaled(
                    self.WIDTH - 4,
                    self.HEIGHT - 4,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                thumb.setPixmap(scaled)
            else:
                thumb.setText("IMG")
        else:
            ext = os.path.splitext(self.display_name or self.source)[1].lower() or "FILE"
            thumb.setObjectName("file_thumb")
            thumb.setText(ext)
            thumb.setToolTip(self.display_name or os.path.basename(self.source))

        layout.addWidget(thumb)

        if not self.is_image:
            name_lbl = QLabel(self.display_name or os.path.basename(self.source))
            name_lbl.setObjectName("file_name_lbl")
            name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            elided = name_lbl.fontMetrics().elidedText(
                name_lbl.text(),
                Qt.TextElideMode.ElideMiddle,
                self.WIDTH - 4,
            )
            name_lbl.setText(elided)
            layout.addWidget(name_lbl)

        remove_btn = QPushButton("×")
        remove_btn.setObjectName("image_remove_btn")
        remove_btn.setFixedSize(20, 20)
        remove_btn.setToolTip(QCoreApplication.translate('AttachmentPreviewStrip', '移除附件'))
        remove_btn.setAccessibleName(remove_btn.toolTip())
        remove_btn.clicked.connect(lambda: self.remove_requested.emit(self.source))
        remove_btn.setParent(self)
        remove_btn.move(self.width() - 22, 2)
        remove_btn.show()

    def set_error(self, error: str) -> None:
        detail = str(error or "").strip()
        tooltip = (
            QCoreApplication.translate('AttachmentPreviewStrip', "粘贴图片")
            if self.source.startswith("data:image")
            else self.display_name or os.path.basename(self.source)
        )
        if detail:
            tooltip = QCoreApplication.translate('AttachmentPreviewStrip', '{tooltip}\n准备失败：{detail}').format(tooltip=tooltip, detail=detail)
        self.setToolTip(tooltip)
        self._thumb.setToolTip(tooltip)


class AttachmentPreviewStrip(QScrollArea):
    """One bounded attachment row; overflow scrolls without widening the composer."""

    remove_requested = pyqtSignal(str)
    visibility_changed = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("image_preview")
        self._items: dict[str, AttachmentPreviewItem] = {}
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(AttachmentPreviewItem.HEIGHT + self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent))
        row = QWidget()
        self.setWidget(row)
        self._layout = QHBoxLayout(row)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self._layout.addStretch()
        self.setVisible(False)

    def add_attachment(
        self,
        source: str,
        *,
        is_image: bool,
        error: str = "",
        display_name: str = "",
    ) -> None:
        if source in self._items:
            self._items[source].set_error(error)
            return

        item = AttachmentPreviewItem(
            source,
            is_image=is_image,
            error=error,
            display_name=display_name,
        )
        item.remove_requested.connect(self.remove_requested.emit)
        was_empty = not self._items
        self._items[source] = item
        self._layout.insertWidget(self._layout.count() - 1, item)
        self.setVisible(True)
        if was_empty:
            self.visibility_changed.emit(True)

    def remove_attachment(self, source: str) -> None:
        item = self._items.pop(source, None)
        if item is not None:
            self._layout.removeWidget(item)
            item.hide()
            item.deleteLater()
        if item is not None and not self._items:
            self.setVisible(False)
            self.visibility_changed.emit(False)

    def clear_attachments(self) -> None:
        for source in list(self._items.keys()):
            self.remove_attachment(source)
