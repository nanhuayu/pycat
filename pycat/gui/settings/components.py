"""Small shared building blocks for settings pages."""

from __future__ import annotations

from collections.abc import Callable

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QRect, QSize, Qt, pyqtSlot
from PyQt6.QtGui import QColor, QCursor, QFont, QFontMetrics, QIcon, QKeySequence, QPainter, QPen, QShortcut
from PyQt6.QtWidgets import (
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pycat.gui.utils.theme import (
    COMPACT_CONTROL_HEIGHT,
    configure_icon_button,
    resolve_accent,
    resolve_theme,
    theme_tokens,
)
from pycat.gui.widgets.capsule import SingleLineLabel

SETTINGS_NAV_WIDTH = 232
RESOURCE_LIST_MINIMUM_WIDTH = 200
RESOURCE_LIST_PREFERRED_WIDTH = 240
RESOURCE_LIST_MAXIMUM_WIDTH = 300


RESOURCE_TITLE_ROLE = Qt.ItemDataRole.UserRole + 20
RESOURCE_SUBTITLE_ROLE = Qt.ItemDataRole.UserRole + 21
RESOURCE_ENABLED_ROLE = Qt.ItemDataRole.UserRole + 22
RESOURCE_STATE_ROLE = Qt.ItemDataRole.UserRole + 23
RESOURCE_TRAILING_ICONS_ROLE = Qt.ItemDataRole.UserRole + 24
RESOURCE_DESCRIPTION_ROLE = Qt.ItemDataRole.UserRole + 25
RESOURCE_TOGGLE_ROLE = Qt.ItemDataRole.UserRole + 26
RESOURCE_BADGE_ROLE = Qt.ItemDataRole.UserRole + 27
RESOURCE_DETAIL_META_ROLE = Qt.ItemDataRole.UserRole + 28


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
        button.setIconSize(QSize(16, 16))
        button.setAccessibleName(tooltip or text)
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
        configure_icon_button(button, icon, label)
        if danger:
            button.setProperty("danger", True)
        button.clicked.connect(lambda _checked=False: callback())
        self._layout.addWidget(button)
        return button

    def add_stretch(self) -> None:
        self._layout.addStretch(1)


class SettingsListDetailLayout(QWidget):
    """One list and detail pane for all settings catalogs; no independent draft state."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("settings_list_detail")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.toolbar_layout = QVBoxLayout()
        self.toolbar_layout.setContentsMargins(0, 0, 0, 0)
        root.addLayout(self.toolbar_layout)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setObjectName("settings_list_detail_splitter")
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(16)
        root.addWidget(self.splitter, 1)
        self._compact = True
        self._detail_requested = False
        self.list_panel = QFrame()
        self.list_panel.setObjectName("settings_list_panel")
        self.list_layout = QVBoxLayout(self.list_panel)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(8)
        self.detail_panel = QFrame()
        self.detail_panel.setObjectName("settings_detail_panel")
        self.detail_layout = QVBoxLayout(self.detail_panel)
        self.detail_layout.setContentsMargins(0, 0, 0, 0)
        self.detail_layout.setSpacing(8)
        self.back_button = QPushButton(QCoreApplication.translate('SettingsComponents', "返回列表"))
        self.back_button.clicked.connect(self.show_list)
        self.detail_title = SingleLineLabel()
        self.detail_title.setObjectName("resource_detail_title")
        self.detail_status = SingleLineLabel()
        self.detail_status.setProperty("muted", True)
        self.detail_status.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.detail_status.setMaximumWidth(200)
        back_row = QHBoxLayout()
        self.header_layout = back_row
        back_row.addWidget(self.back_button)
        back_row.addWidget(self.detail_title, 1)
        back_row.addWidget(self.detail_status)
        self.detail_layout.addLayout(back_row)
        self.splitter.addWidget(self.list_panel)
        self.splitter.addWidget(self.detail_panel)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.list_panel.setMinimumWidth(0)
        self.detail_panel.setMinimumWidth(0)

    def bind(self, widget, toggle=None):
        self.list_widget = widget
        widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        widget.itemClicked.connect(lambda item: self._click(item, toggle))
        widget.itemActivated.connect(lambda _: self.show_detail())
        widget.currentItemChanged.connect(self._sync_selection)
        widget.itemChanged.connect(self._sync_selection)
        if toggle:
            QShortcut(QKeySequence("Space"), widget, context=Qt.ShortcutContext.WidgetShortcut, activated=lambda: toggle() if widget.currentItem() and
                widget.currentItem().data(RESOURCE_TOGGLE_ROLE) else None)

    def _click(self, item, toggle):
        if not item.flags() & Qt.ItemFlag.ItemIsSelectable:
            return
        point = self.list_widget.viewport().mapFromGlobal(QCursor.pos())
        rect = self.list_widget.visualItemRect(item)
        if toggle and not item.data(RESOURCE_BADGE_ROLE) and rect.width() >= 360 and item.data(RESOURCE_TOGGLE_ROLE) and rect.right() - 78 <= point.x() <= rect.right() - 30:
            toggle()
        else:
            self.show_detail()

    def show_detail(self):
        """Open the selection while retaining its existing detail widget."""
        item = self.list_widget.currentItem()
        if item is None:
            return
        self._detail_requested = True
        self._sync_selection()

    def show_list(self):
        """Return to the compact list without discarding the editor draft."""
        self._detail_requested = False
        self._apply_layout()
        self.list_widget.setFocus()

    @pyqtSlot()
    def _sync_selection(self):
        """Project selection metadata, ignoring notifications during teardown."""
        # The view can already be invalid while Qt tears down its model.
        # A native slot owns the connection; also reject partial child teardown.
        if sip.isdeleted(self) or sip.isdeleted(self.list_widget):
            return
        item = self.list_widget.currentItem()
        self.detail_title.setText(str(item.data(RESOURCE_TITLE_ROLE) or item.text()) if item else QCoreApplication.translate('SettingsComponents', "选择条目查看详情"))
        self.detail_status.setText(str(item.data(RESOURCE_DETAIL_META_ROLE) or item.data(RESOURCE_BADGE_ROLE) or "") if item else "")
        self.detail_status.setVisible(bool(self.detail_status.text()))
        self.detail_panel.setEnabled(item is not None)
        if item is None:
            self._detail_requested = False
        self._apply_layout()

    def resizeEvent(self, event):
        """Switch pane geometry without changing the user's selected editor."""
        super().resizeEvent(event)
        compact = self.width() < 800
        if compact != self._compact:
            self._compact = compact
            self.list_panel.setMaximumWidth(16777215 if compact else RESOURCE_LIST_MAXIMUM_WIDTH)
            if not compact:
                self.splitter.setSizes([RESOURCE_LIST_PREFERRED_WIDTH, max(400, self.width() - RESOURCE_LIST_PREFERRED_WIDTH - 16)])
        self._apply_layout()

    def _apply_layout(self):
        """Apply compact/wide visibility from the existing navigation intent."""
        opened = not self._compact or self._detail_requested
        self.list_panel.setVisible(not self._compact or not opened)
        self.detail_panel.setVisible(opened)
        self.back_button.setVisible(self._compact)

    def add_detail_widget(self, widget, *, scrollable=False):
        if scrollable:
            scroll = QScrollArea()
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setWidgetResizable(True)
            scroll.setWidget(widget)
            scroll.setObjectName("settings_detail_scroll")
            self.detail_layout.addWidget(scroll, 1)
            return scroll
        self.detail_layout.addWidget(widget, 1)
        return widget


class SettingsEmptyState(QWidget):
    """Small reusable empty state for settings detail panes."""

    def __init__(self, title: str, description: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("settings_empty_state")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 24, 12, 24)
        layout.setSpacing(4)
        layout.addStretch(1)
        heading = QLabel(str(title or QCoreApplication.translate('SettingsComponents', "暂无内容")))
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


def settings_dialog_layout(dialog, title: str) -> QVBoxLayout:
    """Shared geometry for short settings forms; height follows their content."""
    dialog.setWindowTitle(title)
    dialog.setModal(True)
    dialog.setMinimumWidth(520)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(16, 16, 16, 16)
    layout.setSpacing(12)
    return layout


def build_dialog_button_box(parent=None, *, accept_text=None,
                            accept_button=QDialogButtonBox.StandardButton.Save) -> QDialogButtonBox:
    """Build primary/Cancel buttons, retaining native roles and Enter/Escape.

    ``accept_text`` overrides Save; callers connect acceptance/rejection signals.
    Width follows translated content and no configuration is saved here.
    """
    buttons = QDialogButtonBox(
        accept_button | QDialogButtonBox.StandardButton.Cancel,
        parent,
    )
    save = buttons.button(accept_button)
    cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
    save.setText(accept_text if accept_text is not None else QCoreApplication.translate("DialogButtons", "保存"))
    save.setProperty("primary", True)
    save.setDefault(True)
    cancel.setText(QCoreApplication.translate("DialogButtons", "取消"))
    cancel.setAutoDefault(False)
    # Native styles may add platform-dependent Save/Cancel icons.
    for button in (save, cancel):
        button.setIcon(QIcon())
        button.setAccessibleName(button.text())
        button.setFixedHeight(COMPACT_CONTROL_HEIGHT)
        button.setMinimumWidth(72)
    # QDialogButtonBox polishes its standard buttons before properties are set.
    # Refresh the primary surface immediately, including on an already themed app.
    save.style().unpolish(save)
    save.style().polish(save)
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
        title = str(title or QCoreApplication.translate('SettingsComponents', "未命名"))
        detail = str(detail or "").strip()
        self.setText(title)
        self.setData(RESOURCE_TITLE_ROLE, title)
        self.setData(RESOURCE_SUBTITLE_ROLE, detail)
        self.setData(RESOURCE_ENABLED_ROLE, bool(enabled))
        self.setData(RESOURCE_STATE_ROLE, "enabled" if enabled else "disabled")
        self.setSizeHint(QSize(0, 48 if detail else 42))
        if tooltip:
            self.setToolTip(tooltip)


def set_resource_badge(item: QListWidgetItem, text: str) -> None:
    item.setData(RESOURCE_BADGE_ROLE, text)
    item.setData(Qt.ItemDataRole.AccessibleDescriptionRole, text)


class SettingsResourceDelegate(QStyledItemDelegate):
    """Paint settings resources with one compact, theme-aware hierarchy."""

    def sizeHint(self, option, index):
        """Return a stable row height for visible metadata lines."""
        return QSize(0, 78 if index.data(RESOURCE_DESCRIPTION_ROLE) else 56 if index.data(RESOURCE_SUBTITLE_ROLE) else 42)

    def paint(self, painter: QPainter, option, index) -> None:
        """Render semantic resource state and a visible keyboard focus ring."""
        painter.save()
        rect = option.rect.adjusted(0, 2, 0, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        enabled_value = index.data(RESOURCE_ENABLED_ROLE)
        enabled = True if enabled_value is None else bool(enabled_value)
        title = str(index.data(RESOURCE_TITLE_ROLE) or index.data(Qt.ItemDataRole.DisplayRole) or "")
        subtitle = str(index.data(RESOURCE_SUBTITLE_ROLE) or "").strip()
        description = str(index.data(RESOURCE_DESCRIPTION_ROLE) or "").strip()
        trailing_icons = [
            value
            for value in (index.data(RESOURCE_TRAILING_ICONS_ROLE) or [])
            if isinstance(value, QIcon) and not value.isNull()
        ]
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        icon = icon if isinstance(icon, QIcon) else QIcon()

        owner = self.parent() if isinstance(self.parent(), QWidget) else None
        tokens = theme_tokens(resolve_theme(owner), resolve_accent(owner))
        colors = tokens.colors
        badge = str(index.data(RESOURCE_BADGE_ROLE) or "")
        if selected:
            background = QColor(colors["selected"])
            title_color = QColor(colors["selected_text"])
            subtitle_color = QColor(colors["selected_meta"])
        elif hovered:
            background = QColor(colors["surface_hover"])
            title_color = QColor(colors["text"])
            subtitle_color = QColor(colors["muted"])
        else:
            background = QColor(colors["surface"])
            title_color = QColor(colors["text"])
            subtitle_color = QColor(colors["muted"])

        if not enabled:
            title_color = QColor(colors["muted"])
            subtitle_color = QColor(colors["disabled"])

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(background)
        painter.drawRoundedRect(rect, 6, 6)
        content = rect.adjusted(12, 8, -12, -8)
        icon_size = max(18, option.decorationSize.width())
        icon_width = icon_size + 8 if not icon.isNull() else 0
        trailing_width = len(trailing_icons) * 20
        toggle = bool(index.data(RESOURCE_TOGGLE_ROLE)) and not badge and rect.width() >= 360
        state_width = (52 if toggle else 18 if not enabled and not badge else 0) + trailing_width
        text_left = content.left() + icon_width
        text_width = max(24, content.width() - icon_width - state_width)
        title_font = QFont(option.font)
        title_font.setPixelSize(tokens.font("body"))
        title_font.setWeight(QFont.Weight.Medium)
        subtitle_font = QFont(option.font)
        subtitle_font.setPixelSize(tokens.font("caption"))

        title_height = 19
        title_top = content.top() if subtitle or description else content.top() + max(0, (content.height() - title_height) // 2)
        badge_width = min(QFontMetrics(subtitle_font).horizontalAdvance(badge), max(0, text_width - 48)) if badge else 0
        title_width = text_width - badge_width - (8 if badge else 0)
        title_rect = QRect(text_left, title_top, title_width, title_height)
        if icon_width:
            icon.paint(painter, QRect(content.left(), content.center().y() - icon_size // 2,
                                      icon_size, icon_size))
        painter.setFont(title_font)
        painter.setPen(title_color)
        painter.drawText(
            title_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            QFontMetrics(title_font).elidedText(title, Qt.TextElideMode.ElideRight, title_rect.width()),
        )
        if badge:
            painter.setFont(subtitle_font)
            painter.setPen(subtitle_color)
            painter.drawText(QRect(text_left + text_width - badge_width, title_top, badge_width, title_height),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                QFontMetrics(subtitle_font).elidedText(badge, Qt.TextElideMode.ElideRight, badge_width))

        if description:
            painter.setFont(subtitle_font)
            painter.setPen(subtitle_color)
            painter.drawText(QRect(text_left, content.top() + 22, text_width, 18), Qt.AlignmentFlag.AlignVCenter,
                QFontMetrics(subtitle_font).elidedText(description.replace("\n", " "), Qt.TextElideMode.ElideRight, text_width))
        if subtitle:
            subtitle_rect = QRect(text_left, content.top() + (44 if description else 22), text_width, 17)
            painter.setFont(subtitle_font)
            painter.setPen(subtitle_color)
            painter.drawText(
                subtitle_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                QFontMetrics(subtitle_font).elidedText(subtitle, Qt.TextElideMode.ElideRight, subtitle_rect.width()),
            )

        if trailing_icons:
            left = content.right() - trailing_width + 2
            for offset, trailing_icon in enumerate(trailing_icons):
                trailing_icon.paint(painter, QRect(left + offset * 20, content.center().y() - 8, 16, 16))

        if toggle:
            switch = QRect(rect.right() - 70, rect.center().y() - 9, 32, 18)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(colors["primary"] if enabled else colors["border"]))
            painter.drawRoundedRect(switch, 9, 9)
            painter.setBrush(QColor(colors["surface"]))
            painter.drawEllipse(switch.left() + (17 if enabled else 3), switch.top() + 3, 12, 12)
        elif not enabled and not badge:
            center_x = content.right() - 5
            center_y = content.center().y()
            painter.setPen(QPen(QColor(colors["muted"]), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(center_x - 3, center_y - 4, center_x - 3, center_y + 4)
            painter.drawLine(center_x + 2, center_y - 4, center_x + 2, center_y + 4)
        if option.state & QStyle.StateFlag.State_HasFocus:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(colors["primary"]), 1))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 6, 6)
        painter.restore()


def configure_settings_resource_list(
    widget: QListWidget,
    *,
    minimum_width: int = RESOURCE_LIST_MINIMUM_WIDTH,
) -> QListWidget:
    """Configure a native resource list without changing its model or contents."""
    widget.setObjectName("settings_list")
    widget.setMinimumWidth(max(180, int(minimum_width)))
    widget.setFrameShape(QFrame.Shape.NoFrame)
    widget.setSpacing(2)
    widget.setIconSize(QSize(18, 18))
    widget.setMouseTracking(True)
    widget.setTextElideMode(Qt.TextElideMode.ElideRight)
    widget.setUniformItemSizes(False)
    widget.setItemDelegate(SettingsResourceDelegate(widget))
    return widget
