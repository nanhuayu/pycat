"""System tray integration for the desktop window lifecycle."""
from __future__ import annotations

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon


class TrayController(QObject):
    """Own the tray icon while leaving final shutdown to MainWindow."""

    quit_requested = pyqtSignal()

    def __init__(self, window) -> None:
        super().__init__(window)
        self._window = window
        self._enabled = False
        self._available = bool(QSystemTrayIcon.isSystemTrayAvailable())
        self._tray: QSystemTrayIcon | None = None
        self._menu: QMenu | None = None
        if self._available:
            self._build_tray()

    @property
    def available(self) -> bool:
        return self._available

    @property
    def enabled(self) -> bool:
        return self._enabled and self._available

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        if self._tray is None:
            return
        if self._enabled:
            self._tray.show()
        else:
            self._tray.hide()

    def handle_close_event(self, event, *, force_quit: bool = False) -> bool:
        if force_quit or not self.enabled:
            return False
        self._window.hide()
        event.ignore()
        return True

    def restore_window(self) -> None:
        state = self._window.windowState()
        if state & Qt.WindowState.WindowMinimized:
            state &= ~Qt.WindowState.WindowMinimized
            state |= Qt.WindowState.WindowActive
            self._window.setWindowState(state)
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()

    def dispose(self) -> None:
        if self._tray is not None:
            self._tray.hide()

    def _build_tray(self) -> None:
        app = QApplication.instance()
        icon = self._window.windowIcon()
        if icon.isNull() and app is not None:
            icon = app.windowIcon()

        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip("PyCat Agent")
        self._tray.activated.connect(self._on_activated)

        self._menu = QMenu(self._window)
        show_action = QAction("显示 PyCat", self)
        show_action.triggered.connect(self.restore_window)
        self._menu.addAction(show_action)
        self._menu.addSeparator()
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.quit_requested.emit)
        self._menu.addAction(quit_action)
        self._tray.setContextMenu(self._menu)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self.restore_window()
