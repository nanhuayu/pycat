"""Small reusable PyQt widgets for consistent GUI surfaces."""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QToolButton, QVBoxLayout, QWidget

from gui.utils.icon_manager import Icons
from gui.utils.theme import UI_ICON_SIZE


def repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def set_role(widget: QWidget, **properties: object) -> QWidget:
    for name, value in properties.items():
        widget.setProperty(name, value)
    repolish(widget)
    return widget


class ActionButton(QPushButton):
    """Semantic push button with QSS-driven role properties."""

    def __init__(
        self,
        text: str = "",
        *,
        icon: str | None = None,
        primary: bool = False,
        danger: bool = False,
        secondary: bool = False,
        compact: bool = False,
        parent: QWidget | None = None,
    ):
        super().__init__(text, parent)
        self.setObjectName("action_button")
        self.setProperty("primary", bool(primary))
        self.setProperty("danger", bool(danger))
        self.setProperty("secondary", bool(secondary))
        if compact:
            self.setProperty("density", "compact")
        if icon:
            self.setIcon(Icons.get(icon))
            self.setIconSize(QSize(UI_ICON_SIZE["md"], UI_ICON_SIZE["md"]))


class IconToolButton(QToolButton):
    """Fixed-size icon button used by toolbars and compact panels."""

    def __init__(
        self,
        icon: str,
        tooltip: str = "",
        *,
        tone: str = "muted",
        size: int = 30,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("icon_tool_button")
        self.setProperty("tone", tone)
        self.setIcon(Icons.get_muted(icon) if tone == "muted" else Icons.get(icon))
        self.setIconSize(QSize(UI_ICON_SIZE["md"], UI_ICON_SIZE["md"]))
        self.setToolTip(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(size, size)


class SectionHeader(QWidget):
    """Compact title + optional description row."""

    def __init__(self, title: str, description: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("section_header")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self.title_label = QLabel(str(title or ""))
        self.title_label.setProperty("heading", True)
        layout.addWidget(self.title_label)

        self.description_label = QLabel(str(description or ""))
        self.description_label.setProperty("muted", True)
        self.description_label.setWordWrap(True)
        self.description_label.setVisible(bool(description))
        layout.addWidget(self.description_label)


class EmptyState(QFrame):
    """Small empty-state block for chat/settings/inspector panels."""

    def __init__(self, title: str, description: str = "", *, icon: str | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("empty_state")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        if icon:
            icon_row = QHBoxLayout()
            icon_row.addStretch(1)
            icon_label = QLabel()
            icon_label.setObjectName("empty_state_icon")
            icon_label.setPixmap(Icons.get_muted(icon).pixmap(UI_ICON_SIZE["hero"], UI_ICON_SIZE["hero"]))
            icon_row.addWidget(icon_label)
            icon_row.addStretch(1)
            layout.addLayout(icon_row)

        self.title_label = QLabel(str(title or ""))
        self.title_label.setObjectName("empty_state_title")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title_label)

        self.description_label = QLabel(str(description or ""))
        self.description_label.setObjectName("empty_state_description")
        self.description_label.setProperty("muted", True)
        self.description_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.description_label.setWordWrap(True)
        self.description_label.setVisible(bool(description))
        layout.addWidget(self.description_label)


class StatusPill(QLabel):
    """Compact status label styled by the `tone` property."""

    def __init__(self, text: str = "", *, tone: str = "neutral", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setObjectName("status_pill")
        self.setProperty("tone", tone)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedHeight(22)

    def set_status(self, text: str, tone: str = "neutral") -> None:
        self.setText(str(text or ""))
        self.setProperty("tone", tone)
        repolish(self)
