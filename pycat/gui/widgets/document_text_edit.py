"""Native document editing with logical line numbers and selection actions."""
from PyQt6.QtCore import QEvent, QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QTextDocument, QTextFormat
from PyQt6.QtWidgets import QTextEdit, QWidget

from pycat.gui.utils.theme import resolve_accent, resolve_theme, theme_tokens
from pycat.gui.widgets.document_highlighter import DocumentHighlighter
from pycat.gui.widgets.themed_line_edit import ThemedPlainTextEdit


class CurrentLineHighlight:
    """Use native extra selections; highlighting never modifies document data."""
    def setup_line_highlight(self):
        self.cursorPositionChanged.connect(self.highlight_current_line)
        self.highlight_current_line()

    def highlight_current_line(self):
        selection = QTextEdit.ExtraSelection()
        selection.cursor = self.textCursor()
        selection.cursor.clearSelection()
        selection.format.setBackground(QColor(theme_tokens(resolve_theme(self), resolve_accent(self)).color('surface_alt')))
        selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        self.setExtraSelections([selection])

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in {QEvent.Type.PaletteChange, QEvent.Type.StyleChange}:
            self.highlight_current_line()
            if hasattr(self, 'syntax_highlighter'):
                self.syntax_highlighter.refresh_theme()


class LineNumbers(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def sizeHint(self):
        return QSize(self.editor.gutter_width(), 0)

    def paintEvent(self, event):
        editor = self.editor
        painter = QPainter(self)
        palette = editor.palette()
        painter.fillRect(event.rect(), palette.base())
        painter.setPen(palette.placeholderText().color())
        block = editor.firstVisibleBlock()
        top = round(editor.blockBoundingGeometry(block).translated(editor.contentOffset()).top())
        while block.isValid() and top <= event.rect().bottom():
            height = round(editor.blockBoundingRect(block).height())
            if block.isVisible() and top + height >= event.rect().top():
                painter.drawText(0, top, self.width() - 8, editor.fontMetrics().height(),
                                 Qt.AlignmentFlag.AlignRight, str(block.blockNumber() + 1))
            top += height
            block = block.next()


class DocumentTextEdit(CurrentLineHighlight, ThemedPlainTextEdit):
    selection_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(self.Shape.NoFrame)
        self.line_numbers = LineNumbers(self)
        self.line_numbers_visible = True
        self.blockCountChanged.connect(self._update_gutter)
        self.updateRequest.connect(self._update_numbers)
        self._update_gutter()
        self.setup_line_highlight()
        self.syntax_highlighter = DocumentHighlighter(self)

    def set_syntax(self, suffix):
        self.syntax_highlighter.configure(suffix)

    def gutter_width(self):
        if not self.line_numbers_visible:
            return 0
        return 16 + self.fontMetrics().horizontalAdvance('9') * len(str(max(1, self.blockCount())))

    def set_line_numbers_visible(self, enabled):
        self.line_numbers_visible = bool(enabled)
        self.line_numbers.setVisible(self.line_numbers_visible)
        self._update_gutter()

    def _update_gutter(self, *_):
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)
        area = self.contentsRect()
        self.line_numbers.setGeometry(QRect(area.left(), area.top(), self.gutter_width(), area.height()))
        self.line_numbers.update()

    def _update_numbers(self, rect, dy):
        if dy:
            self.line_numbers.scroll(0, dy)
        else:
            self.line_numbers.update(0, rect.y(), self.line_numbers.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_gutter()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_gutter()

    def set_wrapped(self, enabled):
        self.setLineWrapMode(self.LineWrapMode.WidgetWidth if enabled else self.LineWrapMode.NoWrap)

    def find_text(self, text, *, backwards=False):
        if not text:
            return False
        flags = QTextDocument.FindFlag.FindBackward if backwards else QTextDocument.FindFlag(0)
        if self.find(text, flags):
            return True
        cursor = self.textCursor()
        cursor.movePosition(cursor.MoveOperation.End if backwards else cursor.MoveOperation.Start)
        self.setTextCursor(cursor)
        return self.find(text, flags)

    def createStandardContextMenu(self):
        menu = super().createStandardContextMenu()
        if self.textCursor().hasSelection():
            menu.addSeparator()
            action = menu.addAction(self.tr('加入内容对话'))
            action.triggered.connect(lambda: self.selection_requested.emit(self.textCursor().selectedText().replace('\u2029', '\n')))
        return menu
