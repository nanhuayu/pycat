"""Window-owned capture lifecycle, with no screenshot history or extra runtime."""
from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent, QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import QApplication, QKeySequenceEdit

from pycat.gui.dialogs.screenshot import CaptureOverlay, PinnedImage, ScreenshotDialog
from pycat.gui.runtime.global_shortcut import GlobalShortcut
from pycat.gui.runtime.screen_capture import capture_screens


class ScreenshotController(QObject):
    failed = pyqtSignal(str)
    shortcut_changed = pyqtSignal(str)

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.overlays = []
        self.dialog = None
        self.pins = set()
        self._restore = False
        self._disposed = False
        self._was_active = False
        self._modal = None
        self._focus = None
        self._transparent = []
        self._shortcut = ''
        self.shortcut_status = ''
        self.hotkey = GlobalShortcut(self.start)
        self._focus_connection = QApplication.instance().focusChanged.connect(self._focus_changed)
        self.failed.connect(self._show_failure)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._capture)

    def _show_failure(self, message):
        self.window.chat_view.show_notice(message)

    def bind_shortcut(self, sequence):
        self._shortcut = sequence
        self._focus_changed(None, QApplication.focusWidget())

    def _focus_changed(self, old, current):
        while current is not None:
            if isinstance(current, QKeySequenceEdit):
                self.hotkey.bind('')  # Let the editor record even the currently bound hotkey.
                return
            current = current.parentWidget()
        previous = self.shortcut_status
        error = self.hotkey.bind(self._shortcut)
        # The editable shortcut row already shows the binding. Only exceptional
        # status needs another visible line; normal scope remains in the tooltip.
        self.shortcut_status = error
        if self.shortcut_status != previous:
            self.shortcut_changed.emit(self.shortcut_status)
        if error and self.hotkey.supported and self.shortcut_status != previous:
            self.failed.emit(error)
        action = getattr(self.window, 'capture_action', None)
        if action is not None:
            action.setToolTip(error or (QCoreApplication.translate('ScreenshotController', '全局截图 · {_shortcut}').format(_shortcut=self._shortcut) if self.hotkey.active else QCoreApplication.translate('ScreenshotController', '截图快捷键已停用')))

    def start(self):
        if self._disposed or self.window._shutdown_started or self._timer.isActive() or self.overlays:
            return
        self._restore = self.window.isVisible()
        self._was_active = QApplication.activeWindow() is not None
        if self.dialog is not None:
            self.dialog.close()
        self._modal = QApplication.activeModalWidget()
        self._focus = QApplication.focusWidget()
        if self._modal is not None:
            # Hiding a dialog executing exec() can finish its nested event loop.
            # Keep its modality/lifecycle intact, but exclude our windows from pixels.
            for widget in QApplication.topLevelWidgets():
                if widget.isVisible() and (widget is self.window or self.window.isAncestorOf(widget)):
                    self._transparent.append((widget, widget.windowOpacity()))
                    widget.setWindowOpacity(0)
        else:
            self.window.hide()
        # Let the window/menu disappear before reading pixels from each screen.
        self._timer.start(180)

    def _capture(self):
        try:
            if QApplication.activeModalWidget() is not self._modal:
                raise ValueError(QCoreApplication.translate('ScreenshotController', '当前弹窗已变化，请处理后重新截图。'))
            snapshots = capture_screens()
            for image, geometry in snapshots:
                parent = self._modal if self._modal is not None and not sip.isdeleted(self._modal) else None
                overlay = CaptureOverlay(image, geometry, parent=parent)
                overlay.installEventFilter(self)
                overlay.selected.connect(self.review)
                overlay.cancelled.connect(self.cancel)
                self.overlays.append(overlay)
                overlay.show()
            current = next((item for item in self.overlays if item.geometry().contains(QCursor.pos())), self.overlays[0])
            current.raise_()
            current.activateWindow()
            current.setFocus()
        except (ValueError, OSError, RuntimeError) as exc:
            self.cancel()
            self.failed.emit(str(exc))

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.WindowBlocked and watched in self.overlays:
            # A late SSH authentication/approval dialog must never trap the user
            # behind an input-blocked full-screen image.
            QTimer.singleShot(0, self.cancel)
        return super().eventFilter(watched, event)

    def _clear_overlays(self):
        for overlay in self.overlays:
            overlay.close()
            overlay.deleteLater()
        self.overlays.clear()

    def cancel(self):
        self._timer.stop()
        self._clear_overlays()
        transparent, self._transparent = self._transparent, []
        for widget, opacity in transparent:
            if not sip.isdeleted(widget):
                widget.setWindowOpacity(opacity)
        if self._restore and not self.window._shutdown_started:
            if self._was_active:
                self.window.tray_controller.restore_window()
            else:
                previous = self.window.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
                self.window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
                self.window.show()
                self.window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, previous)
        if self._modal is not None and not sip.isdeleted(self._modal) and self._was_active:
            self._modal.raise_()
            self._modal.activateWindow()
        if self._focus is not None and not sip.isdeleted(self._focus) and self._was_active:
            self._focus.setFocus()
        self._restore = False
        self._modal = self._focus = None

    def review(self, image):
        parent = self._modal if self._modal is not None and not sip.isdeleted(self._modal) else self.window
        self.cancel()
        dialog = ScreenshotDialog(image, ocr_service=self.window.services.ocr_service, parent=parent)
        self.dialog = dialog
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.attach_requested.connect(self._attach)
        dialog.pin_requested.connect(self._pin)
        dialog.finished.connect(self._review_closed)
        if parent is not self.window or self.window.settings_presenter._settings_dialog is not None:
            dialog.attach_button.setEnabled(False)
            dialog.attach_button.setToolTip(QCoreApplication.translate('ScreenshotController', '请先关闭当前弹窗并返回会话；仍可复制图片后粘贴'))
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _review_closed(self):
        self.dialog = None

    def _attach(self, data_url):
        if QApplication.activeModalWidget() is not None or self.window.settings_presenter._settings_dialog is not None:
            self.dialog.notice.setText(QCoreApplication.translate('ScreenshotController', '请先返回会话，或复制图片后粘贴'))
            return
        self.window.tray_controller.restore_window()
        self.window.input_area.add_attachments([data_url])
        self.window.input_area.text_input.setFocus()
        if self.dialog is not None:
            self.dialog.accept()

    def _pin(self, image):
        pin = PinnedImage(image, QApplication.activeModalWidget() or self.window)
        self.pins.add(pin)
        pin.destroyed.connect(lambda: self.pins.discard(pin))
        pin.move(QCursor.pos())
        pin.show()

    def dispose(self):
        if self._disposed:
            return
        self._disposed = True
        # Disconnect the native connection, independent of compiled method
        # wrapper identity. Focus changes must not rebind a disposed hotkey.
        QObject.disconnect(self._focus_connection)
        self.hotkey.dispose()
        self.cancel()
        if self.dialog is not None:
            self.dialog.close()
        for pin in tuple(self.pins):
            pin.close()
        self.pins.clear()
