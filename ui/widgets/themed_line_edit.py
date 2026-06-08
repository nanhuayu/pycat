"""Line edit with a theme-aware standard context menu."""

from __future__ import annotations

from PyQt6.QtWidgets import QLineEdit

from ui.utils.theme import prepare_context_menu


class ThemedLineEdit(QLineEdit):
    """QLineEdit whose standard context menu follows the active PyCat theme."""

    def createStandardContextMenu(self):
        return prepare_context_menu(super().createStandardContextMenu(), self)

    def contextMenuEvent(self, event) -> None:
        menu = self.createStandardContextMenu()
        menu.exec(event.globalPos())
        menu.deleteLater()
