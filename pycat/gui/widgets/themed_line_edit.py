"""Text widgets with a theme-aware standard context menu."""

from __future__ import annotations

from pycat.gui.shortcuts import shortcut_sequence

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence
from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QTextBrowser,
    QTextEdit,
)

from pycat.gui.utils.theme import prepare_context_menu


class ThemedContextMenuMixin:
    """Apply the active PyCat theme whenever a standard text menu opens."""

    def createStandardContextMenu(self):
        return prepare_context_menu(super().createStandardContextMenu(), self)

    def contextMenuEvent(self, event) -> None:
        menu = self.createStandardContextMenu()
        menu.exec(event.globalPos())
        # The owner widget may be destroyed while the menu's event loop ran
        # (e.g. streaming refresh rebuilds message bubbles); guard the teardown.
        try:
            menu.deleteLater()
        except RuntimeError:
            pass


class ThemedLineEdit(ThemedContextMenuMixin, QLineEdit):
    """Theme-aware ``QLineEdit``."""


class SearchLineEdit(ThemedLineEdit):
    """One search field with an unobtrusive shortcut hint when empty."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.shortcut_hint = QLabel(shortcut_sequence("search_conversations"), self)
        self.shortcut_hint.setProperty("muted", True)
        self.shortcut_hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.textChanged.connect(self._sync_hint)
        self._sync_hint()

    def _sync_hint(self):
        visible = not self.text() and bool(self.shortcut_hint.text())
        self.shortcut_hint.setVisible(visible)
        width = self.shortcut_hint.sizeHint().width()
        self.setTextMargins(0, 0, width + 12 if visible else 0, 0)
        self.shortcut_hint.setGeometry(max(0, self.width() - width - 8), 0, width, self.height())

    def set_shortcut_text(self, text):
        self.shortcut_hint.setText(text)
        self._sync_hint()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_hint()


class ThemedTextEdit(ThemedContextMenuMixin, QTextEdit):
    """Theme-aware ``QTextEdit``."""


class ThemedPlainTextEdit(ThemedContextMenuMixin, QPlainTextEdit):
    """Theme-aware ``QPlainTextEdit``."""


class ThemedTextBrowser(ThemedContextMenuMixin, QTextBrowser):
    """Theme-aware ``QTextBrowser``."""


class ThemedSelectableLabel(QLabel):
    """Selectable label with the same themed copy menu as text editors."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

    def createStandardContextMenu(self):
        menu = QMenu(self)
        copy_action = menu.addAction("复制")
        copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        copy_action.setEnabled(self.hasSelectedText())
        copy_action.triggered.connect(self._copy_selected_text)

        select_all_action = menu.addAction("全选")
        select_all_action.setShortcut(QKeySequence.StandardKey.SelectAll)
        select_all_action.setEnabled(bool(self.text()))
        select_all_action.triggered.connect(self._select_all_text)

        prepare_context_menu(menu, self)
        menu.aboutToShow.connect(lambda: prepare_context_menu(menu, self))
        return menu

    def contextMenuEvent(self, event) -> None:
        if not self.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse:
            super().contextMenuEvent(event)
            return
        menu = self.createStandardContextMenu()
        menu.exec(event.globalPos())
        try:
            menu.deleteLater()
        except RuntimeError:
            pass

    def _copy_selected_text(self) -> None:
        QApplication.clipboard().setText(self.selectedText())

    def _select_all_text(self) -> None:
        self.setSelection(0, len(self.text()))
