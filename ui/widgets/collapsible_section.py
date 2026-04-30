from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


class _ClickableHeader(QFrame):
    clicked = pyqtSignal()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class CollapsibleSection(QFrame):
    """Lightweight collapsible section used by side panels and dialogs."""

    toggled = pyqtSignal(bool)

    def __init__(
        self,
        title: str,
        *,
        summary: str = "",
        collapsed: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("collapse_section")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self._collapsed = bool(collapsed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = _ClickableHeader()
        header.setObjectName("collapse_header")
        header.setCursor(Qt.CursorShape.PointingHandCursor)
        header.clicked.connect(self.toggle)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 8, 10, 8)
        header_layout.setSpacing(6)

        self.toggle_btn = QToolButton()
        self.toggle_btn.setObjectName("collapse_toggle")
        self.toggle_btn.setAutoRaise(True)
        self.toggle_btn.setArrowType(Qt.ArrowType.RightArrow)
        self.toggle_btn.setFixedSize(18, 18)
        self.toggle_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.toggle_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.toggle_btn.clicked.connect(self.toggle)
        header_layout.addWidget(self.toggle_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self.title_label = QLabel("")
        self.title_label.setObjectName("collapse_title")
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        header_layout.addWidget(self.title_label, 0, Qt.AlignmentFlag.AlignVCenter)

        header_layout.addStretch(1)

        self.summary_label = QLabel("")
        self.summary_label.setObjectName("collapse_summary")
        self.summary_label.setProperty("muted", True)
        self.summary_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.summary_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.summary_label.setVisible(False)
        header_layout.addWidget(self.summary_label, 0, Qt.AlignmentFlag.AlignVCenter)

        layout.addWidget(header)

        self.body = QWidget()
        self.body.setObjectName("collapse_body")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(10, 0, 10, 10)
        self.body_layout.setSpacing(8)
        layout.addWidget(self.body)

        self.set_title(title)
        self.set_summary(summary)
        self.set_collapsed(self._collapsed)

    def set_title(self, title: str) -> None:
        self._title = str(title or "分组")
        self._refresh_toggle_text()

    def set_summary(self, summary: str) -> None:
        text = str(summary or "").strip()
        self.summary_label.setText(text)
        self.summary_label.setVisible(bool(text))

    def set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = bool(collapsed)
        self.body.setVisible(not self._collapsed)
        self._refresh_toggle_text()
        self.toggled.emit(not self._collapsed)

    def is_collapsed(self) -> bool:
        return self._collapsed

    def toggle(self) -> None:
        self.set_collapsed(not self._collapsed)

    def _refresh_toggle_text(self) -> None:
        self.toggle_btn.setArrowType(Qt.ArrowType.RightArrow if self._collapsed else Qt.ArrowType.DownArrow)
        self.title_label.setText(self._title)
