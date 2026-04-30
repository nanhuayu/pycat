"""Simple image viewer dialog for message thumbnails."""

from __future__ import annotations

from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QScrollArea, QWidget, QHBoxLayout, QPushButton
from PyQt6.QtCore import Qt

from ui.utils.image_loader import load_pixmap


class ImageViewerDialog(QDialog):
    def __init__(self, image_source: str, parent=None):
        super().__init__(parent)
        self._image_source = image_source
        self._pixmap = load_pixmap(image_source)

        self.setWindowTitle("图片预览")
        self.setMinimumSize(720, 520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        # Toolbar
        toolbar = QHBoxLayout()
        toolbar.addStretch()

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        toolbar.addWidget(close_btn)

        layout.addLayout(toolbar)

        self._label = QLabel()
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.addWidget(self._label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        layout.addWidget(scroll)

        if self._pixmap.isNull():
            self._label.setText("图片数据为空或无法解析")
            self._label.setStyleSheet("color: #a1a1aa; font-size: 13px;")
        else:
            # Fit-to-window initially
            self._label.setPixmap(self._pixmap)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._pixmap.isNull():
            return

        # Fit image into available space while keeping aspect ratio
        available_w = max(1, self.width() - 40)
        available_h = max(1, self.height() - 80)
        scaled = self._pixmap.scaled(
            available_w,
            available_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._label.setPixmap(scaled)
