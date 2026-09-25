"""One owned Windows hotkey, delivered on the Qt thread; no keyboard hook or polling."""
from __future__ import annotations

import ctypes
import itertools
import sys
from ctypes import wintypes

from PyQt6.QtCore import QAbstractNativeEventFilter, QCoreApplication, Qt, QTimer
from PyQt6.QtGui import QGuiApplication, QKeySequence


def _windows_api():
    if sys.platform != 'win32' or QGuiApplication.platformName() != 'windows':
        return None
    api = ctypes.WinDLL('user32', use_last_error=True)
    api.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    api.RegisterHotKey.restype = wintypes.BOOL
    api.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    api.UnregisterHotKey.restype = wintypes.BOOL
    api.VkKeyScanW.argtypes = [wintypes.WCHAR]
    api.VkKeyScanW.restype = ctypes.c_short
    return api


class GlobalShortcut(QAbstractNativeEventFilter):
    _ids = itertools.count(0x5100)

    def __init__(self, callback):
        super().__init__()
        self._api = _windows_api()
        self._callback = callback
        self._id = next(self._ids)
        self._sequence = ''
        self.active = False
        QGuiApplication.instance().installNativeEventFilter(self)

    @property
    def supported(self):
        return self._api is not None

    def bind(self, text):
        if self.active and text == self._sequence:
            return ''
        self._release()
        self._sequence = text
        if not text:
            return ''
        if self._api is None:
            return QCoreApplication.translate('GlobalShortcut', '当前平台使用应用内快捷键；全局截图快捷键支持 Windows。')
        sequence = QKeySequence(text)
        if sequence.isEmpty() or sequence.count() != 1:
            return QCoreApplication.translate('GlobalShortcut', '全局截图快捷键需要一个组合键。')
        combination = sequence[0]
        key = int(combination.key())
        modifiers = 0x4000  # MOD_NOREPEAT
        for qt, native in ((Qt.KeyboardModifier.ControlModifier, 2), (Qt.KeyboardModifier.AltModifier, 1),
                           (Qt.KeyboardModifier.ShiftModifier, 4), (Qt.KeyboardModifier.MetaModifier, 8)):
            if combination.keyboardModifiers() & qt:
                modifiers |= native
        special = {Qt.Key.Key_Return: 0x0D, Qt.Key.Key_Enter: 0x0D, Qt.Key.Key_Space: 0x20,
                   Qt.Key.Key_Tab: 0x09, Qt.Key.Key_Escape: 0x1B, Qt.Key.Key_Backspace: 0x08,
                   Qt.Key.Key_Delete: 0x2E, Qt.Key.Key_Insert: 0x2D, Qt.Key.Key_Home: 0x24,
                   Qt.Key.Key_End: 0x23, Qt.Key.Key_PageUp: 0x21, Qt.Key.Key_PageDown: 0x22,
                   Qt.Key.Key_Left: 0x25, Qt.Key.Key_Up: 0x26, Qt.Key.Key_Right: 0x27, Qt.Key.Key_Down: 0x28}
        if Qt.Key.Key_F1 <= key <= Qt.Key.Key_F24:
            key = 0x70 + key - Qt.Key.Key_F1
        elif key in special:
            key = special[key]
        elif 0x20 <= key <= 0x7E:
            mapped = self._api.VkKeyScanW(chr(key).lower())
            if mapped == -1:
                return QCoreApplication.translate('GlobalShortcut', '此按键无法注册为全局快捷键，请使用字母、数字或功能键。')
            key = mapped & 0xFF
            # VkKeyScan encodes Shift / Ctrl / Alt as 1 / 2 / 4.
            modifiers |= sum(native for mask, native in ((1, 4), (2, 2), (4, 1)) if (mapped >> 8) & mask)
        else:
            return QCoreApplication.translate('GlobalShortcut', '此按键无法注册为全局快捷键，请使用字母、数字或功能键。')
        if not self._api.RegisterHotKey(None, self._id, modifiers, key):
            return QCoreApplication.translate('GlobalShortcut', '截图快捷键 {text} 被占用或由系统保留；可在“设置 → 快捷键”更换，仍可从托盘截图。').format(text=text)
        self.active = True
        return ''

    def dispatch(self, identifier):
        if self.active and identifier == self._id:
            QTimer.singleShot(0, self._activate)
            return True
        return False

    def _activate(self):
        if self.active:
            self._callback()

    def nativeEventFilter(self, event_type, message):
        if bytes(event_type) in (b'windows_generic_MSG', b'windows_dispatcher_MSG'):
            event = wintypes.MSG.from_address(int(message))
            if event.message == 0x0312 and self.dispatch(event.wParam):  # WM_HOTKEY
                return True, 0
        return False, 0

    def _release(self):
        if self.active:
            self._api.UnregisterHotKey(None, self._id)
            self.active = False

    def dispose(self):
        self._release()
        QGuiApplication.instance().removeNativeEventFilter(self)
