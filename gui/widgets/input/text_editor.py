"""Custom text editor and completion popup extracted from input_area.py."""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from PyQt6.QtWidgets import QListWidget, QTextEdit
from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtGui import QKeyEvent, QTextCursor

from core.commands import CommandRegistry
from core.commands.mentions import MentionCandidate, MentionKind, MentionQuery
from gui.shortcuts import matches_shortcut
from gui.utils.image_utils import extract_images_from_mime


logger = logging.getLogger(__name__)


class FileCompleterPopup(QListWidget):
    """Popup list for file/mention completion."""

    file_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("file_completer_popup")
        self.setWindowFlags(Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.hide()

    def show_completions(self, files: List[str], point):
        self.clear()
        if not files:
            self.hide()
            return

        self.addItems(files)
        self.setCurrentRow(0)

        h = min(len(files) * 26 + 4, 200)
        w = 200
        self.resize(w, h)
        self.move(point)
        self.show()
        self.raise_()

    def keyPressEvent(self, event):
        super().keyPressEvent(event)


class MessageTextEdit(QTextEdit):
    """Custom text edit with Ctrl+Enter and inline mention completion."""

    _HISTORY_LIMIT = 50

    send_requested = pyqtSignal()
    attachments_received = pyqtSignal(list)
    file_reference_added = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.work_dir: Optional[str] = None
        self._command_registry: Optional[CommandRegistry] = None
        self._mention_context_provider: Optional[Callable[[], Dict[str, Any]]] = None
        self._completion_candidates: dict[str, MentionCandidate] = {}
        self._active_query: Optional[MentionQuery] = None

        self.completer_popup = FileCompleterPopup(self)
        self.completer_popup.itemClicked.connect(self._on_completion_selected)
        self.completer_popup.hide()

        self._completing = False
        self._completion_prefix = ""
        self._completion_start_pos = -1
        self._history_entries: list[str] = []
        self._history_index: int | None = None
        self._history_draft = ""
        self._applying_history = False
        self.textChanged.connect(self._on_text_changed)

    def remember_history_entry(self, text: str) -> None:
        """Keep exact composer input for transient shell-style navigation."""

        value = str(text or "").strip()
        if not value:
            return
        if not self._history_entries or self._history_entries[-1] != value:
            self._history_entries.append(value)
            if len(self._history_entries) > self._HISTORY_LIMIT:
                self._history_entries = self._history_entries[-self._HISTORY_LIMIT :]
        self._reset_history_navigation()

    def set_history_entries(self, entries: List[str] | None) -> None:
        """Replace navigation history when the active conversation changes."""

        normalized: list[str] = []
        for item in entries or []:
            value = str(item or "").strip()
            if value and (not normalized or normalized[-1] != value):
                normalized.append(value)
        self._history_entries = normalized[-self._HISTORY_LIMIT :]
        self._reset_history_navigation()

    def _on_text_changed(self) -> None:
        if not self._applying_history:
            self._reset_history_navigation()

    def _reset_history_navigation(self) -> None:
        self._history_index = None
        self._history_draft = ""

    def _handle_history_key(self, event: QKeyEvent) -> bool:
        if event.modifiers() != Qt.KeyboardModifier.NoModifier:
            return False
        if event.key() not in {Qt.Key.Key_Up, Qt.Key.Key_Down}:
            return False

        if self._history_index is None:
            if event.key() != Qt.Key.Key_Up or not self._history_entries:
                return False
            cursor = self.textCursor()
            if self.toPlainText() and (cursor.hasSelection() or cursor.position() != 0):
                return False
            self._history_draft = self.toPlainText()
            self._history_index = len(self._history_entries)

        if event.key() == Qt.Key.Key_Up:
            self._history_index = max(0, self._history_index - 1)
            self._apply_history_text(self._history_entries[self._history_index])
            return True

        next_index = self._history_index + 1
        if next_index >= len(self._history_entries):
            draft = self._history_draft
            self._history_index = None
            self._history_draft = ""
            self._apply_history_text(draft)
        else:
            self._history_index = next_index
            self._apply_history_text(self._history_entries[self._history_index])
        return True

    def _apply_history_text(self, text: str) -> None:
        self._applying_history = True
        try:
            self.setPlainText(str(text or ""))
            cursor = self.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.setTextCursor(cursor)
        finally:
            self._applying_history = False

    def set_work_dir(self, path: str):
        self.work_dir = path

    def configure_command_registry(
        self,
        registry: CommandRegistry,
        context_provider: Callable[[], Dict[str, Any]],
    ) -> None:
        self._command_registry = registry
        self._mention_context_provider = context_provider

    def insertFromMimeData(self, source):
        try:
            data_urls, file_paths = extract_images_from_mime(source)
            sources: list[str] = []
            sources.extend(data_urls)
            sources.extend(file_paths)
            if sources:
                self.attachments_received.emit(sources)
                return
        except Exception as exc:
            logger.debug("Failed to extract pasted image attachments in text editor: %s", exc)

        super().insertFromMimeData(source)

    def keyPressEvent(self, event: QKeyEvent):
        if self.completer_popup.isVisible():
            if event.key() == Qt.Key.Key_Down:
                row = self.completer_popup.currentRow()
                if row < self.completer_popup.count() - 1:
                    self.completer_popup.setCurrentRow(row + 1)
                return
            elif event.key() == Qt.Key.Key_Up:
                row = self.completer_popup.currentRow()
                if row > 0:
                    self.completer_popup.setCurrentRow(row - 1)
                return
            elif event.key() in (
                Qt.Key.Key_Enter,
                Qt.Key.Key_Return,
                Qt.Key.Key_Tab,
            ):
                if self.completer_popup.currentItem():
                    self._on_completion_selected(self.completer_popup.currentItem())
                return
            elif event.key() == Qt.Key.Key_Escape:
                self.completer_popup.hide()
                return

        if matches_shortcut(event, "send_message"):
            self.send_requested.emit()
            return

        if self._handle_history_key(event):
            return

        if self._history_index is not None and event.key() not in {
            Qt.Key.Key_Shift,
            Qt.Key.Key_Control,
            Qt.Key.Key_Alt,
            Qt.Key.Key_Meta,
        }:
            self._reset_history_navigation()

        super().keyPressEvent(event)
        self._check_completion()

    def _check_completion(self):
        if not self._command_registry or not self._mention_context_provider:
            self.completer_popup.hide()
            return

        cursor = self.textCursor()
        command_result = self._command_registry.get_command_candidates(
            self.toPlainText(),
            cursor.position(),
            self._mention_context_provider(),
        )
        if command_result:
            query, candidates = command_result
            self._show_completer(query, candidates)
            return

        result = self._command_registry.get_mention_candidates(
            self.toPlainText(),
            cursor.position(),
            self._mention_context_provider(),
        )
        if result:
            query, candidates = result
            self._show_completer(query, candidates)
            return

        self._active_query = None
        self._completion_candidates.clear()
        self.completer_popup.hide()

    def _show_completer(
        self, query: MentionQuery, candidates: List[MentionCandidate]
    ):
        if not candidates:
            self.completer_popup.hide()
            return

        rect = self.cursorRect()
        point = self.viewport().mapToGlobal(rect.bottomLeft())
        point.setY(point.y() + 5)

        self._active_query = query
        self._completion_prefix = query.prefix
        self._completion_start_pos = query.start_pos
        self._completion_candidates = {
            candidate.label: candidate for candidate in candidates
        }
        self.completer_popup.show_completions(
            [candidate.label for candidate in candidates], point
        )

    def _on_completion_selected(self, item):
        if (
            not self._command_registry
            or not self._mention_context_provider
            or not self._active_query
        ):
            return

        display_name = item.text()
        candidate = self._completion_candidates.get(display_name)
        if not display_name or candidate is None:
            return

        cursor = self.textCursor()
        cursor.setPosition(self._active_query.start_pos)
        cursor.setPosition(
            self._active_query.end_pos, QTextCursor.MoveMode.KeepAnchor
        )

        if not candidate.terminal:
            cursor.insertText(f"{self._active_query.trigger}{candidate.value}")
            self.setTextCursor(cursor)
            self.completer_popup.hide()
            self._check_completion()
            return

        if candidate.kind != MentionKind.FILE:
            insert_text = (
                candidate.insert_text
                or f"{self._active_query.trigger}{candidate.value}"
            )
            cursor.insertText(insert_text)
            self.setTextCursor(cursor)
            self.completer_popup.hide()
            self._active_query = None
            self._completion_candidates.clear()
            return

        full_path = self._command_registry.resolve_mention_candidate(
            candidate,
            self._mention_context_provider(),
        )
        if not full_path:
            self.completer_popup.hide()
            return

        cursor.removeSelectedText()
        self.setTextCursor(cursor)
        self.completer_popup.hide()
        self._active_query = None
        self._completion_candidates.clear()
        self.file_reference_added.emit(full_path)
