"""Text widgets with a theme-aware standard context menu."""

from __future__ import annotations

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

from gui.utils.theme import prepare_context_menu


class ThemedContextMenuMixin:
    """Apply the active PyCat theme whenever a standard text menu opens."""

    def createStandardContextMenu(self):
        return prepare_context_menu(super().createStandardContextMenu(), self)

    def contextMenuEvent(self, event) -> None:
        menu = self.createStandardContextMenu()
        menu.exec(event.globalPos())
        menu.deleteLater()


class ThemedLineEdit(ThemedContextMenuMixin, QLineEdit):
    """Theme-aware ``QLineEdit``."""


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
        menu.deleteLater()

    def _copy_selected_text(self) -> None:
        QApplication.clipboard().setText(self.selectedText())

    def _select_all_text(self) -> None:
        self.setSelection(0, len(self.text()))
