"""One shortcut catalog for actions, settings and visible keyboard hints."""
from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import QT_TRANSLATE_NOOP, QCoreApplication, Qt
from PyQt6.QtGui import QKeyEvent, QKeySequence


@dataclass(frozen=True)
class ShortcutSpec:
    id: str
    source_label: str
    sequence: str
    group: str
    editable: bool = True

    @property
    def label(self) -> str:
        return QCoreApplication.translate("Shortcuts", self.source_label)


SHORTCUTS: tuple[ShortcutSpec, ...] = (
    ShortcutSpec("new_conversation", QT_TRANSLATE_NOOP("Shortcuts", "新建对话"), "Ctrl+N", "对话"),
    ShortcutSpec("search_conversations", QT_TRANSLATE_NOOP("Shortcuts", "搜索对话或设置"), "Ctrl+K", "对话"),
    ShortcutSpec("send_message", QT_TRANSLATE_NOOP("Shortcuts", "发送消息"), "Ctrl+Enter", "对话"),
    ShortcutSpec("cancel_generation", QT_TRANSLATE_NOOP("Shortcuts", "停止生成 / 取消"), "Escape", "对话", False),
    ShortcutSpec("message_previous", QT_TRANSLATE_NOOP("Shortcuts", "上一条消息"), "Alt+Up", "导航"),
    ShortcutSpec("message_next", QT_TRANSLATE_NOOP("Shortcuts", "下一条消息"), "Alt+Down", "导航"),
    ShortcutSpec("message_first", QT_TRANSLATE_NOOP("Shortcuts", "滚动到顶部"), "Alt+Home", "导航"),
    ShortcutSpec("message_last", QT_TRANSLATE_NOOP("Shortcuts", "滚动到底部"), "Alt+End", "导航"),
    ShortcutSpec("capture", QT_TRANSLATE_NOOP("Shortcuts", "截图"), "Ctrl+Shift+S", "应用"),
    ShortcutSpec("import_conversation", QT_TRANSLATE_NOOP("Shortcuts", "导入会话"), "Ctrl+I", "应用"),
    ShortcutSpec("open_settings", QT_TRANSLATE_NOOP("Shortcuts", "打开设置"), "Ctrl+,", "应用"),
    ShortcutSpec("quit", QT_TRANSLATE_NOOP("Shortcuts", "退出 PyCat"), "Ctrl+Q", "应用"),
)

_BY_ID = {spec.id: spec for spec in SHORTCUTS}


def shortcut_sequence(shortcut_id: str, overrides=None) -> str:
    spec = _BY_ID[shortcut_id]
    return str((overrides or {}).get(shortcut_id, spec.sequence)) if spec.editable else spec.sequence


def _key_identity(sequence: QKeySequence):
    if sequence.isEmpty():
        return None
    combination = sequence[0]
    key = combination.key()
    if key == Qt.Key.Key_Enter:
        key = Qt.Key.Key_Return
    return int(key), combination.keyboardModifiers().value


def matches_shortcut(event: QKeyEvent, shortcut_id: str, overrides=None) -> bool:
    expected = QKeySequence(shortcut_sequence(shortcut_id, overrides))
    return not expected.isEmpty() and _key_identity(QKeySequence(event.keyCombination())) == _key_identity(expected)


def validate_shortcuts(overrides) -> dict[str, str]:
    """Normalize user overrides and reject ambiguous or text-editing bindings."""
    used = {}
    result = {}
    reserved = {_key_identity(QKeySequence(key)) for key in
                ('Ctrl+A', 'Ctrl+C', 'Ctrl+X', 'Ctrl+V', 'Ctrl+Z', 'Ctrl+Y', 'Ctrl+Shift+Z', 'Ctrl+S', 'Alt+F4')}
    for spec in SHORTCUTS:
        text = shortcut_sequence(spec.id, overrides)
        sequence = QKeySequence(text, QKeySequence.SequenceFormat.PortableText)
        if text and (sequence.isEmpty() or sequence.count() != 1 or sequence[0].key() == Qt.Key.Key_unknown):
            raise ValueError(QCoreApplication.translate("Shortcuts", "{action}：请使用一个有效的组合键").format(action=spec.label))
        identity = _key_identity(sequence)
        if identity is not None:
            key, modifiers = identity
            modified = modifiers & (Qt.KeyboardModifier.ControlModifier.value | Qt.KeyboardModifier.AltModifier.value | Qt.KeyboardModifier.MetaModifier.value)
            function_key = Qt.Key.Key_F1 <= key <= Qt.Key.Key_F35
            if spec.editable and (identity in reserved or (not modified and not function_key)):
                raise ValueError(QCoreApplication.translate("Shortcuts", "{action}：该按键用于输入或系统操作，请使用 Ctrl/Alt 组合键或功能键").format(action=spec.label))
            if identity in used:
                raise ValueError(QCoreApplication.translate("Shortcuts", "{action}与“{other}”的快捷键冲突").format(action=spec.label, other=used[identity]))
            used[identity] = spec.label
        normalized = sequence.toString(QKeySequence.SequenceFormat.PortableText)
        if spec.editable and sequence != QKeySequence(spec.sequence):
            result[spec.id] = normalized
    return result
