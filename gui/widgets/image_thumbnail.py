"""Small image thumbnail used by chat message cards."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QLabel, QSizePolicy

from gui.utils.image_loader import load_pixmap


class ImageThumbnail(QLabel):
    """Clickable fixed-size preview for an attached image."""

    clicked = pyqtSignal()

    def __init__(self, image_source: str, parent=None):
        super().__init__(parent)
        self._image_source = str(image_source or "")
        self.setObjectName("image_thumbnail")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedSize(80, 80)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._load()

    def _load(self) -> None:
        if not self._image_source:
            self.setText("Image")
            self.setProperty("state", "placeholder")
            return

        pixmap = load_pixmap(self._image_source)
        if pixmap.isNull():
            self.setText("Image")
            self.setProperty("state", "error")
            return

        scaled = pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(scaled)
        self.setProperty("state", "image")

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)
