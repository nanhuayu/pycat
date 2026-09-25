"""Small image thumbnail used by chat message cards."""

from __future__ import annotations

from PyQt6.QtCore import Qt, QThreadPool, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QLabel, QSizePolicy

from pycat.gui.runtime.background_job import BackgroundJob
from pycat.gui.utils.image_loader import load_pixmap


class ImageThumbnail(QLabel):
    """Clickable fixed-size preview for an attached image."""

    clicked = pyqtSignal()

    def __init__(self, image_source: str, parent=None, *, load_image=None):
        super().__init__(parent)
        self._image_source = str(image_source or "")
        self.setObjectName("image_thumbnail")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedSize(80, 80)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._job = None
        if load_image is None:
            self._load()
        else:
            self.setText("加载中")
            self._job = BackgroundJob(load_image)
            self._job.signals.finished.connect(self._image_loaded)
            self.destroyed.connect(lambda _=None, job=self._job: job.abandon())
            QThreadPool.globalInstance().start(self._job)

    @pyqtSlot(object, object)
    def _image_loaded(self, image, error):
        self._job = None
        if error is not None or image is None or image.isNull():
            self.setText("Image")
            self.setProperty("state", "error")
            return
        self.setPixmap(QPixmap.fromImage(image).scaled(self.size(),
            Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        self.setProperty("state", "image")

    def _load(self) -> None:
        if not self._image_source:
            self.setText("Image")
            self.setProperty("state", "placeholder")
            return

        pixmap = load_pixmap(self._image_source, max_size=self.size())
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
