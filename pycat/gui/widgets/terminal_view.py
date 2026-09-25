"""Native Qt terminal surface. pyte owns VT parsing; Qt owns painting and IME."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

import pyte
from PyQt6.QtCore import QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QKeySequence, QPainter
from PyQt6.QtWidgets import QAbstractScrollArea, QApplication, QMenu

from pycat.gui.utils.theme import TERMINAL_COLORS, prepare_context_menu


class _Screen(pyte.HistoryScreen):
    def __init__(self, reply):
        self._reply = reply
        self._primary = None
        super().__init__(100, 30, history=1000)

    def write_process_input(self, data):
        self._reply(data)

    def resize(self, lines=None, columns=None):
        if lines and lines < self.lines:
            # pyte always clips at the top, even when all content is near the
            # top and the lower rows are empty. Keep the cursor and its context.
            removed = max(0, self.cursor.y - lines + 1)
            previous = dict(self.buffer)
            if self._primary is None:
                self.history.top.extend(previous.get(y, {}) for y in range(removed))
            self.buffer.clear()
            self.buffer.update((y - removed, row) for y, row in previous.items()
                               if removed <= y < removed + lines)
            self.cursor.y -= removed
            self.lines = lines
            self.set_margins()
        super().resize(lines=lines, columns=columns)
        self.cursor.x = min(self.cursor.x, self.columns - 1)
        self.dirty.update(range(self.lines))

    def set_mode(self, *modes, **kwargs):
        if kwargs.get("private") and any(m in (47, 1047, 1049) for m in modes) and self._primary is None:
            self._primary = deepcopy((self.buffer, self.cursor, self.history, self.margins))
            self.buffer.clear()
            self.history.top.clear()
            self.history.bottom.clear()
            self.cursor_position()
        super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes, **kwargs):
        if kwargs.get("private") and any(m in (47, 1047, 1049) for m in modes) and self._primary is not None:
            self.buffer, self.cursor, self.history, self.margins = self._primary
            self._primary = None
            self.dirty.update(range(self.lines))
        super().reset_mode(*modes, **kwargs)


@dataclass(frozen=True)
class TerminalFrame:
    rows: tuple
    columns: int
    lines: int
    history: int
    cursor: object
    mode: frozenset
    default_char: object
    responses: tuple[str, ...]


class TerminalView(QAbstractScrollArea):
    input_ready = pyqtSignal(str)
    response_ready = pyqtSignal(str)
    dimensions_changed = pyqtSignal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("shell_terminal")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled)
        font = QFont()
        font.setFamilies(["Consolas", "Menlo", "DejaVu Sans Mono", "monospace"])
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFixedPitch(True)
        font.setPointSize(11)
        self._terminal_font = font
        self.setFont(font)
        self._cell_width = max(1, QFontMetrics(font).horizontalAdvance("M"))
        self._cell_height = QFontMetrics(font).height() + 2
        self._ascent = QFontMetrics(font).ascent() + 1
        self._responses = []
        self.screen = _Screen(self._responses.append)
        self._stream = pyte.Stream(self.screen)
        self.dimensions = (self.screen.columns, self.screen.lines)
        self._frame = self.prepare_output("")
        self._input_enabled = False
        self._preedit = ""
        self._selection = None
        self.verticalScrollBar().valueChanged.connect(lambda _: self.viewport().update())
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumSize(240, 120)

    def set_input_enabled(self, enabled):
        self._input_enabled = bool(enabled)
        self.viewport().update()

    def feed(self, text: str):
        """Synchronous helper for a caller already owning the parser (tests)."""
        self.apply_frame(self.prepare_output(text, self.dimensions))

    def prepare_output(self, text, dimensions=None):
        """Worker-only parser. Never reads or changes a Qt property."""
        if dimensions and dimensions != (self.screen.columns, self.screen.lines):
            self.screen.resize(columns=dimensions[0], lines=dimensions[1])
        self._stream.feed(text)
        frame = TerminalFrame(
            tuple(dict(row) for row in self.screen.history.top) + tuple(
                dict(self.screen.buffer[y]) for y in range(self.screen.lines)),
            self.screen.columns, self.screen.lines, len(self.screen.history.top),
            deepcopy(self.screen.cursor), frozenset(self.screen.mode), self.screen.default_char,
            tuple(self._responses),
        )
        self._responses.clear()
        self.screen.dirty.clear()
        return frame

    def apply_frame(self, frame, *, send_responses=True):
        bar = self.verticalScrollBar()
        at_bottom = bar.value() == bar.maximum()
        self._frame = frame
        bar.setRange(0, frame.history)
        bar.setPageStep(frame.lines)
        if at_bottom:
            bar.setValue(bar.maximum())
        self.viewport().update()
        if send_responses:
            for response in frame.responses:
                self.response_ready.emit(response)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        columns = max(2, min(300, (self.viewport().width() - 16) // self._cell_width))
        rows = max(1, min(150, (self.viewport().height() - 12) // self._cell_height))
        if (columns, rows) != self.dimensions:
            self.dimensions = (columns, rows)
            self.dimensions_changed.emit(columns, rows)

    @classmethod
    def _color(cls, name, default):
        if name == "default":
            return QColor(TERMINAL_COLORS[default])
        value = TERMINAL_COLORS.get(name, "#" + name)
        color = QColor(value)
        return color if color.isValid() else QColor(TERMINAL_COLORS[default])

    def _rows(self):
        return self._frame.rows

    def paintEvent(self, event):
        painter = QPainter(self.viewport())
        painter.fillRect(event.rect(), QColor(TERMINAL_COLORS["background"]))
        painter.setFont(self._terminal_font)
        rows, start = self._rows(), self.verticalScrollBar().value()
        selection = sorted(self._selection) if self._selection else None
        frame = self._frame
        for y, row in enumerate(rows[start:start + frame.lines]):
            x = 0
            while x < frame.columns:
                cell = row.get(x, frame.default_char)
                if not cell.data:
                    x += 1
                    continue
                foreground = self._color(cell.fg, "foreground")
                background = self._color(cell.bg, "background")
                if cell.reverse:
                    foreground, background = background, foreground
                selected = selection and selection[0] <= (start + y, x) < selection[1]
                if selected:
                    background = QColor(TERMINAL_COLORS["selection"])
                end = x + 1
                text = cell.data
                # Combine ordinary monospace runs; wide/combining glyphs keep
                # explicit cell positions instead of trusting fallback advances.
                if len(text) == 1 and text.isascii():
                    style = cell[1:]
                    while end < frame.columns:
                        following = row.get(end, frame.default_char)
                        marked = selection and selection[0] <= (start + y, end) < selection[1]
                        if following[1:] != style or marked != selected or len(following.data) != 1 or not following.data.isascii():
                            break
                        text += following.data
                        end += 1
                elif end < frame.columns and row.get(end, frame.default_char).data == "":
                    end += 1
                rect = QRect(8 + x * self._cell_width, 6 + y * self._cell_height,
                             self._cell_width * (end - x), self._cell_height)
                painter.fillRect(rect, background)
                font = QFont(self._terminal_font)
                font.setBold(cell.bold)
                font.setItalic(cell.italics)
                font.setUnderline(cell.underscore)
                font.setStrikeOut(cell.strikethrough)
                painter.setFont(font)
                painter.setPen(foreground)
                painter.drawText(QPoint(rect.x(), rect.y() + self._ascent), text)
                x = end
        if start == self.verticalScrollBar().maximum() and not frame.cursor.hidden:
            rect = self._cursor_rect()
            painter.setPen(QColor(TERMINAL_COLORS["cursor" if self._input_enabled else "inactive_cursor"]))
            painter.drawRect(rect.adjusted(0, 0, -1, -1))
            if self._preedit:
                painter.drawText(QPoint(rect.x(), rect.y() + self._ascent), self._preedit)

    def _cursor_rect(self):
        return QRect(8 + min(self._frame.cursor.x, self._frame.columns - 1) * self._cell_width,
                     6 + self._frame.cursor.y * self._cell_height, self._cell_width, self._cell_height)

    def keyPressEvent(self, event):
        control = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        has_selection = self._selection and self._selection[0] != self._selection[1]
        if (control and shift and event.key() == Qt.Key.Key_C) or (event.matches(QKeySequence.StandardKey.Copy) and has_selection):
            self.copy_selection()
            return
        if event.matches(QKeySequence.StandardKey.Paste) or (control and shift and event.key() == Qt.Key.Key_V):
            self.paste()
            return
        if not self._input_enabled:
            return
        keys = {Qt.Key.Key_Return: "\r", Qt.Key.Key_Enter: "\r", Qt.Key.Key_Backspace: "\x7f",
                Qt.Key.Key_Tab: "\t", Qt.Key.Key_Backtab: "\x1b[Z", Qt.Key.Key_Escape: "\x1b",
                Qt.Key.Key_Up: "\x1b[A", Qt.Key.Key_Down: "\x1b[B", Qt.Key.Key_Right: "\x1b[C",
                Qt.Key.Key_Left: "\x1b[D", Qt.Key.Key_Home: "\x1b[H", Qt.Key.Key_End: "\x1b[F",
                Qt.Key.Key_Delete: "\x1b[3~", Qt.Key.Key_Insert: "\x1b[2~",
                Qt.Key.Key_F1: "\x1bOP", Qt.Key.Key_F2: "\x1bOQ", Qt.Key.Key_F3: "\x1bOR", Qt.Key.Key_F4: "\x1bOS",
                Qt.Key.Key_F5: "\x1b[15~", Qt.Key.Key_F6: "\x1b[17~", Qt.Key.Key_F7: "\x1b[18~", Qt.Key.Key_F8: "\x1b[19~",
                Qt.Key.Key_F9: "\x1b[20~", Qt.Key.Key_F10: "\x1b[21~", Qt.Key.Key_F11: "\x1b[23~", Qt.Key.Key_F12: "\x1b[24~",
                Qt.Key.Key_PageUp: "\x1b[5~", Qt.Key.Key_PageDown: "\x1b[6~"}
        text = keys.get(event.key(), event.text())
        if 1 << 5 in self._frame.mode and text in ("\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D"):
            text = text.replace("[", "O")
        if control and Qt.Key.Key_A <= event.key() <= Qt.Key.Key_Z:
            text = chr(event.key() - Qt.Key.Key_A + 1)
        elif event.modifiers() & Qt.KeyboardModifier.AltModifier and text:
            text = "\x1b" + text
        if text:
            self._selection = None
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
            self.input_ready.emit(text)

    def inputMethodEvent(self, event):
        if self._input_enabled:
            if event.commitString():
                self.input_ready.emit(event.commitString())
            self._preedit = event.preeditString()
            self.viewport().update()
        event.accept()

    def inputMethodQuery(self, query):
        if query == Qt.InputMethodQuery.ImCursorRectangle:
            return self._cursor_rect()
        if query == Qt.InputMethodQuery.ImEnabled:
            return self._input_enabled
        return super().inputMethodQuery(query)

    def _cell_at(self, point):
        return (self.verticalScrollBar().value() + max(0, (point.y() - 6) // self._cell_height),
                max(0, min(self._frame.columns, (point.x() - 8) // self._cell_width)))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            cell = self._cell_at(event.position().toPoint())
            self._selection = (cell, cell)
            self.setFocus()
            self.viewport().update()

    def mouseMoveEvent(self, event):
        if self._selection and event.buttons() & Qt.MouseButton.LeftButton:
            self._selection = (self._selection[0], self._cell_at(event.position().toPoint()))
            self.viewport().update()

    def copy_selection(self):
        if not self._selection:
            return
        (first_y, first_x), (last_y, last_x) = sorted(self._selection)
        rows, lines = self._rows(), []
        for y in range(first_y, min(last_y + 1, len(rows))):
            left, right = first_x if y == first_y else 0, last_x if y == last_y else self._frame.columns
            lines.append("".join(rows[y].get(x, self._frame.default_char).data for x in range(left, right)).rstrip())
        QApplication.clipboard().setText("\n".join(lines))

    def paste(self):
        if self._input_enabled:
            text = QApplication.clipboard().text().replace("\r\n", "\n").replace("\n", "\r")
            if 2004 << 5 in self._frame.mode:
                text = "\x1b[200~" + text + "\x1b[201~"
            self.input_ready.emit(text)

    def contextMenuEvent(self, event):
        menu = prepare_context_menu(QMenu(self), self)
        menu.addAction("复制", self.copy_selection).setEnabled(bool(self._selection))
        menu.addAction("粘贴", self.paste).setEnabled(self._input_enabled)
        menu.exec(event.globalPos())
