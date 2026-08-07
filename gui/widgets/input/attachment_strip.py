"""Attachment preview components extracted from input_area.py."""
from __future__ import annotations

import os

from PyQt6.QtWidgets import (
    QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QWidget,
    QSizePolicy,
)
from PyQt6.QtCore import pyqtSignal, Qt

from gui.utils.image_loader import load_pixmap


class AttachmentPreviewItem(QFrame):
    """Preview item for attached images or files."""

    remove_requested = pyqtSignal(str)

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
        self.setFixedSize(60, 56)

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
                    56,
                    38,
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
                56,
            )
            name_lbl.setText(elided)
            layout.addWidget(name_lbl)

        remove_btn = QPushButton("×")
        remove_btn.setObjectName("image_remove_btn")
        remove_btn.setFixedSize(14, 14)
        remove_btn.clicked.connect(lambda: self.remove_requested.emit(self.source))
        remove_btn.setParent(self)
        remove_btn.move(self.width() - 16, 2)
        remove_btn.show()

    def set_error(self, error: str) -> None:
        detail = str(error or "").strip()
        tooltip = (
            "粘贴图片"
            if self.source.startswith("data:image")
            else self.display_name or os.path.basename(self.source)
        )
        if detail:
            tooltip = f"{tooltip}\n准备失败：{detail}"
        self.setToolTip(tooltip)
        self._thumb.setToolTip(tooltip)


class AttachmentPreviewStrip(QWidget):
    """Preview strip for attached files/images."""

    remove_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("image_preview")
        self._items: dict[str, AttachmentPreviewItem] = {}

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(4, 0, 4, 0)
        self._layout.setSpacing(4)
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
        self._items[source] = item
        self._layout.insertWidget(self._layout.count() - 1, item)
        self.setVisible(True)

    def remove_attachment(self, source: str) -> None:
        item = self._items.pop(source, None)
        if item is not None:
            item.deleteLater()
        if not self._items:
            self.setVisible(False)

    def clear_attachments(self) -> None:
        for source in list(self._items.keys()):
            self.remove_attachment(source)
