"""Small shared building blocks for settings pages."""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import QRect, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPen
from PyQt6.QtWidgets import (
    QDialogButtonBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from gui.utils.theme import resolve_accent, resolve_theme, theme_tokens


SETTINGS_NAV_WIDTH = 184
RESOURCE_LIST_MINIMUM_WIDTH = 200
RESOURCE_LIST_PREFERRED_WIDTH = 208
RESOURCE_LIST_MAXIMUM_WIDTH = 280


RESOURCE_TITLE_ROLE = Qt.ItemDataRole.UserRole + 20
RESOURCE_SUBTITLE_ROLE = Qt.ItemDataRole.UserRole + 21
RESOURCE_ENABLED_ROLE = Qt.ItemDataRole.UserRole + 22
RESOURCE_STATE_ROLE = Qt.ItemDataRole.UserRole + 23


class SettingsActionBar(QWidget):
    """Consistent compact command row used by settings list pages."""

    def __init__(self, parent=None, *, spacing: int = 6):
        super().__init__(parent)
        self.setObjectName("settings_action_bar")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(spacing)

    def add_action(
        self,
        text: str,
        icon: QIcon,
        callback: Callable[[], None],
        *,
        danger: bool = False,
        primary: bool = False,
        tooltip: str = "",
    ) -> QPushButton:
        button = QPushButton(str(text or ""))
        button.setObjectName("settings_action_btn")
        button.setIcon(icon)
        if danger:
            button.setProperty("danger", True)
        if primary:
            button.setProperty("primary", True)
        if tooltip:
            button.setToolTip(tooltip)
        button.clicked.connect(lambda _checked=False: callback())
        self._layout.addWidget(button)
        return button

    def add_widget(self, widget: QWidget) -> None:
        self._layout.addWidget(widget)

    def add_icon_action(
        self,
        label: str,
        icon: QIcon,
        callback: Callable[[], None],
        *,
        danger: bool = False,
    ) -> QToolButton:
        """Add a compact icon command with accessible text and a tooltip."""

        button = QToolButton()
        button.setObjectName("toolbar_btn")
        button.setIcon(icon)
        button.setFixedSize(30, 30)
        button.setIconSize(QSize(18, 18))
        button.setToolTip(str(label or ""))
        button.setAccessibleName(str(label or ""))
        if danger:
            button.setProperty("danger", True)
        button.clicked.connect(lambda _checked=False: callback())
        self._layout.addWidget(button)
        return button

    def add_stretch(self) -> None:
        self._layout.addStretch(1)


class SettingsListDetailLayout(QWidget):
    """Shared two-pane surface for a selectable list and its details."""

    def __init__(
        self,
        list_title: str,
        detail_title: str,
        parent=None,
        *,
        list_stretch: int = 2,
        detail_stretch: int = 3,
        spacing: int = 12,
        list_minimum_width: int = RESOURCE_LIST_MINIMUM_WIDTH,
        list_preferred_width: int = RESOURCE_LIST_PREFERRED_WIDTH,
    ):
        super().__init__(parent)
        self.setObjectName("settings_list_detail")
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(max(4, int(spacing // 2)))
        root.addWidget(self.splitter)

        self.list_panel = QFrame()
        self.list_panel.setObjectName("settings_list_panel")
        self.list_panel.setMinimumWidth(max(180, int(list_minimum_width)))
        self.list_panel.setMaximumWidth(RESOURCE_LIST_MAXIMUM_WIDTH)
        self.list_layout = QVBoxLayout(self.list_panel)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(6)
        self.list_title_label = QLabel(str(list_title or "列表"))
        self.list_title_label.setObjectName("settings_pane_title")
        self.list_layout.addWidget(self.list_title_label)
        self.splitter.addWidget(self.list_panel)

        self.detail_group = QGroupBox(str(detail_title or "详情"))
        self.detail_group.setMinimumWidth(360)
        self.detail_layout = QVBoxLayout(self.detail_group)
        self.detail_layout.setContentsMargins(10, 10, 10, 10)
        self.detail_layout.setSpacing(8)
        self.splitter.addWidget(self.detail_group)
        self.splitter.setStretchFactor(0, list_stretch)
        self.splitter.setStretchFactor(1, detail_stretch)
        self.splitter.setSizes([max(180, int(list_preferred_width)), max(420, int(list_preferred_width * 2.2))])

    def set_list_title(self, title: str) -> None:
        self.list_title_label.setText(str(title or "列表"))

    def add_detail_widget(self, widget: QWidget, *, scrollable: bool = False) -> QWidget:
        """Add one editor while keeping resource-page scrolling predictable."""

        if not scrollable:
            self.detail_layout.addWidget(widget, 1)
            return widget

        scroll = QScrollArea()
        scroll.setObjectName("settings_detail_scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(widget)
        self.detail_layout.addWidget(scroll, 1)
        return scroll


class SettingsEmptyState(QWidget):
    """Small reusable empty state for settings detail panes."""

    def __init__(self, title: str, description: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("settings_empty_state")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 24, 12, 24)
        layout.setSpacing(4)
        layout.addStretch(1)
        heading = QLabel(str(title or "暂无内容"))
        heading.setProperty("heading", True)
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(heading)
        if description:
            body = QLabel(str(description))
            body.setProperty("muted", True)
            body.setWordWrap(True)
            body.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(body)
        layout.addStretch(1)


def build_dialog_button_box(parent=None) -> QDialogButtonBox:
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
        parent,
    )
    save = buttons.button(QDialogButtonBox.StandardButton.Save)
    cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
    save.setText("保存")
    save.setProperty("primary", True)
    cancel.setText("取消")
    return buttons


class SettingsStatusListItem(QListWidgetItem):
    """Theme-aware status item shared by model, skill, channel, and MCP lists."""

    def __init__(self, payload: object = None):
        super().__init__()
        self.payload = payload
        self.setData(Qt.ItemDataRole.UserRole, payload)

    def set_status(
        self,
        title: str,
        *,
        enabled: bool,
        detail: str = "",
        tooltip: str = "",
        two_lines: bool = False,
    ) -> None:
        del two_lines
        title = str(title or "未命名")
        detail = str(detail or "").strip()
        self.setText(title)
        self.setData(RESOURCE_TITLE_ROLE, title)
        self.setData(RESOURCE_SUBTITLE_ROLE, detail)
        self.setData(RESOURCE_ENABLED_ROLE, bool(enabled))
        self.setData(RESOURCE_STATE_ROLE, "enabled" if enabled else "disabled")
        self.setSizeHint(QSize(0, 48 if detail else 42))
        if tooltip:
            self.setToolTip(tooltip)


class SettingsResourceDelegate(QStyledItemDelegate):
    """Paint settings resources with one compact, theme-aware hierarchy."""

    def sizeHint(self, option, index):
        return QSize(0, 48 if str(index.data(RESOURCE_SUBTITLE_ROLE) or "").strip() else 42)

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        rect = option.rect.adjusted(3, 1, -3, -1)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        enabled_value = index.data(RESOURCE_ENABLED_ROLE)
        enabled = True if enabled_value is None else bool(enabled_value)
        title = str(index.data(RESOURCE_TITLE_ROLE) or index.data(Qt.ItemDataRole.DisplayRole) or "")
        subtitle = str(index.data(RESOURCE_SUBTITLE_ROLE) or "").strip()
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        icon = icon if isinstance(icon, QIcon) else QIcon()

        owner = self.parent() if isinstance(self.parent(), QWidget) else None
        tokens = theme_tokens(resolve_theme(owner), resolve_accent(owner))
        colors = tokens.colors
        if selected:
            background = QColor(colors["selected"])
            title_color = QColor(colors["selected_text"])
            subtitle_color = QColor(colors["selected_meta"])
            border_color = QColor(colors["selected_border"])
        elif hovered:
            background = QColor(colors["surface_hover"])
            title_color = QColor(colors["text"])
            subtitle_color = QColor(colors["muted"])
            border_color = QColor(colors["border"])
        else:
            background = QColor(colors["surface"])
            title_color = QColor(colors["text"])
            subtitle_color = QColor(colors["muted"])
            border_color = background

        if not enabled:
            title_color = QColor(colors["muted"])
            subtitle_color = QColor(colors["disabled"])

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(background)
        painter.drawRoundedRect(rect, 6, 6)
        if selected or hovered:
            painter.setPen(QPen(border_color, 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(0, 0, -1, -1), 6, 6)

        content = rect.adjusted(9, 4, -9, -4)
        icon_width = 24 if not icon.isNull() else 0
        state_width = 18 if not enabled else 0
        text_left = content.left() + icon_width
        text_width = max(24, content.width() - icon_width - state_width)
        title_font = QFont(option.font)
        title_font.setPixelSize(tokens.font("body"))
        title_font.setWeight(QFont.Weight.Medium)
        subtitle_font = QFont(option.font)
        subtitle_font.setPixelSize(tokens.font("caption"))

        title_height = 19
        title_top = content.top() if subtitle else content.top() + max(0, (content.height() - title_height) // 2)
        title_rect = QRect(text_left, title_top, text_width, title_height)
        if icon_width:
            pixmap = icon.pixmap(18, 18)
            painter.drawPixmap(content.left(), content.center().y() - 9, pixmap)
        painter.setFont(title_font)
        painter.setPen(title_color)
        painter.drawText(
            title_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            QFontMetrics(title_font).elidedText(title, Qt.TextElideMode.ElideRight, title_rect.width()),
        )

        if subtitle:
            subtitle_rect = QRect(text_left, content.top() + 21, text_width, 17)
            painter.setFont(subtitle_font)
            painter.setPen(subtitle_color)
            painter.drawText(
                subtitle_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                QFontMetrics(subtitle_font).elidedText(subtitle, Qt.TextElideMode.ElideRight, subtitle_rect.width()),
            )

        if not enabled:
            center_x = content.right() - 5
            center_y = content.center().y()
            painter.setPen(QPen(QColor(colors["muted"]), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(center_x - 3, center_y - 4, center_x - 3, center_y + 4)
            painter.drawLine(center_x + 2, center_y - 4, center_x + 2, center_y + 4)
        painter.restore()


def configure_settings_resource_list(
    widget: QListWidget,
    *,
    minimum_width: int = RESOURCE_LIST_MINIMUM_WIDTH,
) -> QListWidget:
    widget.setObjectName("settings_list")
    widget.setMinimumWidth(max(180, int(minimum_width)))
    widget.setSpacing(1)
    widget.setMouseTracking(True)
    widget.setTextElideMode(Qt.TextElideMode.ElideRight)
    widget.setUniformItemSizes(False)
    widget.setItemDelegate(SettingsResourceDelegate(widget))
    return widget
