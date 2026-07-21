"""Read-only catalog for application keyboard shortcuts."""
from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeyEvent, QKeySequence


@dataclass(frozen=True)
class ShortcutSpec:
    id: str
    label: str
    sequence: str
    group: str


SHORTCUTS: tuple[ShortcutSpec, ...] = (
    ShortcutSpec("new_conversation", "新建会话", "Ctrl+N", "对话"),
    ShortcutSpec("send_message", "发送消息", "Ctrl+Enter", "对话"),
    ShortcutSpec("cancel_generation", "停止生成", "Escape", "对话"),
    ShortcutSpec("import_conversation", "导入会话", "Ctrl+I", "应用"),
    ShortcutSpec("open_settings", "打开设置", "Ctrl+,", "应用"),
    ShortcutSpec("quit", "退出 PyCat", "Ctrl+Q", "应用"),
)

_BY_ID = {spec.id: spec for spec in SHORTCUTS}


def shortcut_sequence(shortcut_id: str) -> str:
    return _BY_ID[shortcut_id].sequence


def matches_shortcut(event: QKeyEvent, shortcut_id: str) -> bool:
    if shortcut_id == "send_message":
        return (
            event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}
            and event.modifiers() == Qt.KeyboardModifier.ControlModifier
        )
    return QKeySequence(event.keyCombination()) == QKeySequence(shortcut_sequence(shortcut_id))
