"""Compact object presentation shared by chat and Inspector item views."""
from PyQt6.QtCore import QEvent, QModelIndex, QPersistentModelIndex, QRect, QSize, Qt
from PyQt6.QtGui import QColor, QContextMenuEvent, QPainter, QPalette
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMenu,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

from pycat.gui.utils.display_text import single_line
from pycat.gui.utils.theme import (
    COMPACT_CONTROL_HEIGHT,
    prepare_context_menu,
    resolve_accent,
    resolve_theme,
    theme_tokens,
)


def capsule_height(metrics, lines=1):
    return max(COMPACT_CONTROL_HEIGHT, metrics.height() * lines + 12)


class CapsuleRow(QFrame):
    """Widget rows share item-view geometry, even when no action buttons exist."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(4)
        self.setFixedHeight(capsule_height(self.fontMetrics()))

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in {QEvent.Type.FontChange, QEvent.Type.StyleChange}:
            self.setFixedHeight(capsule_height(self.fontMetrics()))


class SingleLineLabel(QLabel):
    """Elide at paint time, retaining normalized text and a complete tooltip."""

    def __init__(self, text="", parent=None, *, max_characters=0):
        super().__init__(parent)
        self.max_characters = max_characters
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(False)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.setText(text)

    def setText(self, text):
        self.setToolTip(str(text or ""))
        self.setAccessibleName(str(text or ""))
        super().setText(single_line(text))

    def minimumSizeHint(self):
        return QSize(0, super().minimumSizeHint().height())

    def display_text(self):
        value = self.text()
        if self.max_characters > 0 and len(value) > self.max_characters:
            value = value[:self.max_characters - 1].rstrip() + "…"
        metrics = self.fontMetrics()
        width = max(0, self.contentsRect().width())
        budget = width
        result = metrics.elidedText(value, Qt.TextElideMode.ElideRight, budget)
        # Fallback fonts can round a mixed-script elision wider than its budget.
        while budget > 0 and (excess := metrics.horizontalAdvance(result) - width) > 0:
            budget = max(0, budget - excess)
            result = metrics.elidedText(value, Qt.TextElideMode.ElideRight, budget)
        return result

    def paintEvent(self, event):
        painter = QPainter(self)
        self.style().drawItemText(painter, self.contentsRect(),
                                 int(self.alignment()) | int(Qt.TextFlag.TextSingleLine),
                                 self.palette(), self.isEnabled(), self.display_text(), QPalette.ColorRole.WindowText)


def text_lines(text, metrics, width, *, maximum=2):
    """Bound display text without altering the item's complete accessible text."""
    text = single_line(text)
    width = max(1, width)
    if maximum == 1 or metrics.horizontalAdvance(text) <= width:
        return [metrics.elidedText(text, Qt.TextElideMode.ElideRight, width)]
    low, high = 1, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if metrics.horizontalAdvance(text[:middle]) <= width:
            low = middle
        else:
            high = middle - 1
    return [text[:low], metrics.elidedText(text[low:].lstrip(), Qt.TextElideMode.ElideRight, width)]


class CapsuleLabel(QLabel):
    """A bounded title for rows that contain real widgets and buttons."""

    def __init__(self, text="", *, maximum=1, parent=None):
        super().__init__(parent)
        self._full_text, self._maximum = "", maximum
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(False)
        self.setMinimumWidth(0)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setText(text)

    def setText(self, text):
        self._full_text = str(text or "")
        self.setToolTip(self._full_text)
        self.setAccessibleName(self._full_text)
        self._refresh()

    def _refresh(self):
        lines = text_lines(self._full_text, self.fontMetrics(), self.contentsRect().width(), maximum=self._maximum)
        super().setText("\n".join(lines))
        self.setFixedHeight(self.fontMetrics().height() * len(lines) + (12 if len(lines) > 1 else 0))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in {QEvent.Type.FontChange, QEvent.Type.StyleChange} and hasattr(self, "_full_text"):
            self._refresh()


class CapsuleDelegate(QStyledItemDelegate):
    """Draw metadata-only lists with the same geometry and theme as object rows."""

    DetailRole = Qt.ItemDataRole.UserRole + 1

    @staticmethod
    def _lines(option, index, width):
        detail = str(index.data(CapsuleDelegate.DetailRole) or "")
        title = text_lines(index.data(Qt.ItemDataRole.DisplayRole), option.fontMetrics, width, maximum=1)
        if detail:
            title.append(option.fontMetrics.elidedText(single_line(detail), Qt.TextElideMode.ElideRight, max(1, width)))
        return title

    def sizeHint(self, option, index):
        view = option.widget or self.parent()
        width = view.viewport().width()
        lines = self._lines(option, index, width - 32)
        return QSize(0, capsule_height(option.fontMetrics, len(lines)) + 4)

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        tokens = theme_tokens(resolve_theme(opt.widget), resolve_accent(opt.widget))
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        hovered = bool(opt.state & QStyle.StateFlag.State_MouseOver)
        focused = bool(opt.state & QStyle.StateFlag.State_HasFocus)
        rect = opt.rect.adjusted(1, 2, -1, -2)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(tokens.color("selected" if selected else "surface_hover" if hovered else "surface")))
        painter.setPen(QColor(tokens.color("primary" if focused else "selected_border" if selected or hovered else "border")))
        painter.drawRoundedRect(rect, tokens.corner("md"), tokens.corner("md"))
        inset, gap = tokens.space("xs"), tokens.space("xs")
        opt.icon.paint(painter, QRect(rect.left() + inset, rect.center().y() - 8, 16, 16))
        left = rect.left() + inset + 16 + gap
        width = rect.right() - inset - left
        lines = self._lines(opt, index, width)
        height = opt.fontMetrics.height()
        top = rect.center().y() - len(lines) * height // 2
        painter.setFont(opt.font)
        detail = bool(index.data(self.DetailRole))
        for position, line in enumerate(lines):
            painter.setPen(QColor(tokens.color("muted" if detail and position else "text")))
            painter.drawText(QRect(left, top + position * height, width, height),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, line)
        painter.restore()


class CapsuleList(QListWidget):
    """An ordinary item view with object-row painting and keyboard context access."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("capsule_list")
        self.setFrameShape(QListWidget.Shape.NoFrame)
        self.setItemDelegate(CapsuleDelegate(self))
        self.setMouseTracking(True)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setWordWrap(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.populate_menu = None

    def contextMenuEvent(self, event):
        keyboard = event.reason() == QContextMenuEvent.Reason.Keyboard
        item = self.currentItem() if keyboard else self.itemAt(event.pos())
        if item is None:
            return
        self.setCurrentItem(item)
        index = QPersistentModelIndex(self.indexFromItem(item))
        menu = prepare_context_menu(QMenu(self), self)
        if self.populate_menu:
            self.populate_menu(menu, item)
        else:
            menu.addAction("查看", lambda: self.itemActivated.emit(self.itemFromIndex(QModelIndex(index))) if index.isValid() else None)
        menu.aboutToHide.connect(menu.deleteLater)
        position = self.visualItemRect(item).center() if keyboard else event.pos()
        menu.exec(self.viewport().mapToGlobal(position))
        event.accept()
