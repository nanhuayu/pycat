"""One display body for Markdown, plain text, images and PDF pages."""
from PyQt6.QtCore import QEvent, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.theme import resolve_accent, resolve_theme
from pycat.gui.widgets.document_text_edit import CurrentLineHighlight, DocumentTextEdit
from pycat.gui.widgets.markdown_view import MarkdownView, markdown_css
from pycat.gui.widgets.themed_line_edit import ThemedSelectableLabel
from pycat.models.contracts.library import ANNOTATION_COLOR


class DocumentMarkdownView(CurrentLineHighlight, MarkdownView):
    selection_requested = pyqtSignal(str)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setup_line_highlight()

    def createStandardContextMenu(self):
        menu = super().createStandardContextMenu()
        if self.textCursor().hasSelection():
            menu.addSeparator()
            action = menu.addAction(self.tr('加入内容对话'))
            action.triggered.connect(lambda: self.selection_requested.emit(self.textCursor().selectedText().replace('\u2029', '\n')))
        return menu


class DocumentHtmlView(DocumentMarkdownView):
    """Native rich text with no script engine, resource loading or file navigation."""
    def __init__(self):
        self._markup = ''
        super().__init__(auto_height=False)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.anchorClicked.connect(lambda url: self.scrollToAnchor(url.fragment()) if url.toString().startswith('#') else None)

    def set_markup(self, markup):
        self._markup = markup
        self._refresh_theme()

    def _refresh_theme(self):
        theme, accent = resolve_theme(self), resolve_accent(self)
        self._rendered_theme = f'{theme}:{accent}'
        self.setHtml(markdown_css(theme, accent) + self._markup)

    def event(self, event):
        result = super().event(event)
        if event.type() in {QEvent.Type.PaletteChange, QEvent.Type.ParentChange, QEvent.Type.StyleChange, QEvent.Type.Polish}:
            if self._markup and f'{resolve_theme(self)}:{resolve_accent(self)}' != self._rendered_theme:
                self._theme_timer.start(0)
        return result

    def loadResource(self, kind, url):
        return None


class ImageCanvas(QGraphicsView):
    """Transform one bounded raster in Qt; dragging never re-reads the file."""
    zoom_changed = pyqtSignal(float)
    annotations_changed = pyqtSignal(object)
    annotation_selected = pyqtSignal(int)
    annotation_edit_requested = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.viewport().setObjectName("image_viewer_viewport")
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setToolTip("拖动平移 · Ctrl + 滚轮缩放")
        self.setAccessibleName("图片或 PDF 页面预览")
        self.auto_fit = True
        self.zoom_factor = 1.0
        self.annotations = []
        self.selected_annotation = -1
        self._overlays = []
        self._drawing = self._drag_origin = None
        self.annotation_mode = False

    def set_image(self, image, *, preserve_zoom=False):
        self.scene().clear()
        self.annotations, self._overlays = [], []
        self.selected_annotation = -1
        self._drawing = self._drag_origin = None
        if image is None:
            self.setSceneRect(0, 0, 0, 0)
            return
        item = self.scene().addPixmap(QPixmap.fromImage(image))
        self.setSceneRect(item.boundingRect())
        if preserve_zoom and not self.auto_fit:
            self.set_zoom(self.zoom_factor)
        else:
            self.fit_image()

    def set_zoom(self, factor):
        if not self.scene().items():
            return
        self.auto_fit = False
        self.zoom_factor = max(0.05, min(8.0, float(factor)))
        self.resetTransform()
        self.scale(self.zoom_factor, self.zoom_factor)
        self.zoom_changed.emit(self.zoom_factor)

    def fit_image(self):
        self.auto_fit = True
        if self.scene().items():
            self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
            self.zoom_factor = self.transform().m11()
            if self.zoom_factor > 1:
                self.resetTransform()
                self.zoom_factor = 1.0
            self.zoom_changed.emit(self.zoom_factor)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.set_zoom(self.zoom_factor * (1.2 ** (event.angleDelta().y() / 120)))
            event.accept()
        else:
            super().wheelEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.auto_fit:
            self.fit_image()

    def set_annotation_mode(self, enabled):
        self.annotation_mode = bool(enabled)
        self.setDragMode(self.DragMode.NoDrag if enabled else self.DragMode.ScrollHandDrag)
        self.viewport().setCursor(Qt.CursorShape.CrossCursor if enabled else Qt.CursorShape.OpenHandCursor)

    def set_annotations(self, annotations, *, selected=-1):
        for item in self._overlays:
            self.scene().removeItem(item)
        self._overlays = []
        self.annotations = [dict(entry) for entry in annotations]
        self.selected_annotation = selected if 0 <= selected < len(self.annotations) else -1
        bounds = self.sceneRect()
        for number, entry in enumerate(self.annotations, 1):
            x, y, width, height = entry['rect']
            rect = QRectF(x * bounds.width(), y * bounds.height(), width * bounds.width(), height * bounds.height())
            color = QColor(entry.get('color') or ANNOTATION_COLOR)
            active = number - 1 == self.selected_annotation
            pen = QPen(color, 3 if active else 2)
            pen.setCosmetic(True)
            overlay = self.scene().addRect(rect, pen)
            if active:
                fill = QColor(color)
                fill.setAlpha(24)
                overlay.setBrush(fill)
            overlay.setData(0, number - 1)
            overlay.setToolTip(entry.get('note', ''))
            label = self.scene().addSimpleText(str(number))
            label.setPos(rect.topLeft())
            label.setBrush(QColor(entry.get('color') or ANNOTATION_COLOR))
            label.setFlag(label.GraphicsItemFlag.ItemIgnoresTransformations)
            label.setData(0, number - 1)
            self._overlays.extend((overlay, label))

    def select_annotation(self, index):
        self.set_annotations(self.annotations, selected=index)
        self.annotation_selected.emit(self.selected_annotation)

    def update_annotation(self, index, note):
        if self.isEnabled() and 0 <= index < len(self.annotations):
            entries = [dict(entry) for entry in self.annotations]
            entries[index]['note'] = note
            self.set_annotations(entries, selected=index)
            self.annotations_changed.emit(self.annotations)

    def delete_annotation(self, index=None):
        index = self.selected_annotation if index is None else index
        if self.isEnabled() and 0 <= index < len(self.annotations):
            entries = [dict(entry) for entry in self.annotations]
            del entries[index]
            selected = min(index, len(entries) - 1)
            self.set_annotations(entries, selected=selected)
            self.annotations_changed.emit(self.annotations)
            self.annotation_selected.emit(selected)

    def _annotation_at(self, point):
        bounds = self.sceneRect()
        for index in range(len(self.annotations) - 1, -1, -1):
            x, y, width, height = self.annotations[index]['rect']
            if QRectF(x * bounds.width(), y * bounds.height(), width * bounds.width(), height * bounds.height()).contains(point):
                return index
        return -1

    def event(self, event):
        if (event.type() == QEvent.Type.ShortcutOverride and self.selected_annotation >= 0
                and event.key() in {Qt.Key.Key_Delete, Qt.Key.Key_Backspace}
                and event.modifiers() == Qt.KeyboardModifier.NoModifier):
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event):
        if (self.selected_annotation >= 0 and event.key() in {Qt.Key.Key_Delete, Qt.Key.Key_Backspace}
                and event.modifiers() == Qt.KeyboardModifier.NoModifier):
            self.delete_annotation()
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            point = self.mapToScene(event.position().toPoint())
            item = self.itemAt(event.position().toPoint())
            label = isinstance(item, QGraphicsSimpleTextItem) and item.data(0) is not None
            if not self.annotation_mode or label:
                index = int(item.data(0)) if label else self._annotation_at(point)
                self.select_annotation(index)
                if index >= 0:
                    event.accept()
                    return
        if self.annotation_mode and event.button() == Qt.MouseButton.LeftButton and self.sceneRect().contains(self.mapToScene(event.position().toPoint())):
            self._drag_origin = self.mapToScene(event.position().toPoint())
            pen = QPen(QColor(ANNOTATION_COLOR), 2)
            pen.setCosmetic(True)
            self._drawing = self.scene().addRect(QRectF(self._drag_origin, self._drag_origin), pen)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drawing is not None:
            rect = QRectF(self._drag_origin, self.mapToScene(event.position().toPoint())).normalized().intersected(self.sceneRect())
            self._drawing.setRect(rect)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drawing is not None:
            rect = QRectF(self._drag_origin, self.mapToScene(event.position().toPoint())).normalized().intersected(self.sceneRect())
            self.scene().removeItem(self._drawing)
            self._drawing = self._drag_origin = None
            bounds = self.sceneRect()
            if rect.width() > 3 and rect.height() > 3 and len(self.annotations) < 100:
                entry = {'rect': [rect.x() / bounds.width(), rect.y() / bounds.height(),
                                  rect.width() / bounds.width(), rect.height() / bounds.height()],
                         'note': '', 'color': ANNOTATION_COLOR}
                self.set_annotations([*self.annotations, entry], selected=len(self.annotations))
                self.annotations_changed.emit(self.annotations)
                self.annotation_selected.emit(self.selected_annotation)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        index = self._annotation_at(self.mapToScene(event.position().toPoint()))
        if index >= 0:
            if self._drawing is not None:
                self.scene().removeItem(self._drawing)
                self._drawing = self._drag_origin = None
            self.select_annotation(index)
            self.annotation_edit_requested.emit(index)
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)


class ContentViewer(QWidget):
    zoom_changed = pyqtSignal(float)
    selection_requested = pyqtSignal(str)
    system_open_requested = pyqtSignal()
    reveal_requested = pyqtSignal()
    attach_requested = pyqtSignal()
    def __init__(self, parent=None):
        super().__init__(parent)
        self.kind, self.text, self._image = "", "", None
        self.page, self.pages = 1, 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.source_visible = False
        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.raw = DocumentTextEdit()
        self.raw.setObjectName("content_text_preview")
        self.raw.setReadOnly(True)
        self.stack.addWidget(self.raw)
        self.markdown = DocumentMarkdownView(auto_height=False)
        self.markdown.selection_requested.connect(self.selection_requested)
        self.raw.selection_requested.connect(self.selection_requested)
        self.markdown.setObjectName("content_preview_surface")
        self.markdown.document().setDocumentMargin(10)
        self.markdown.viewport().setObjectName("content_preview_viewport")
        self.stack.addWidget(self.markdown)
        self.picture = ImageCanvas()
        self.picture.setMinimumSize(1, 1)
        self.picture.setObjectName("image_viewer_surface")
        self.picture.zoom_changed.connect(self.zoom_changed)
        self.stack.addWidget(self.picture)
        self.html = DocumentHtmlView()
        self.html.setObjectName('content_preview_surface')
        self.html.selection_requested.connect(self.selection_requested)
        self.stack.addWidget(self.html)
        self.overview = QScrollArea()
        self.overview.setObjectName('file_overview')
        self.overview.setWidgetResizable(True)
        self.overview.setFrameShape(self.overview.Shape.NoFrame)
        body = QWidget()
        column = QVBoxLayout(body)
        column.setContentsMargins(24, 24, 24, 24)
        column.setSpacing(14)
        column.addStretch()
        icon = QLabel()
        icon.setPixmap(Icons.get_muted(Icons.FILE).pixmap(56, 56))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(icon)
        self.file_name = ThemedSelectableLabel()
        font = self.file_name.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self.file_name.setFont(font)
        self.file_metadata = ThemedSelectableLabel()
        notice = ThemedSelectableLabel(self.tr('此格式暂不支持内容预览。可用系统程序打开，或在主对话中处理。'))
        self.file_path = ThemedSelectableLabel()
        for label in (self.file_name, self.file_metadata, notice, self.file_path):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            column.addWidget(label)
        self.file_metadata.setProperty('muted', True)
        self.file_path.setProperty('muted', True)
        self.overview_chat = QPushButton(self.tr('在主对话中处理'))
        self.overview_chat.setIcon(Icons.get_muted(Icons.CHAT))
        self.overview_chat.clicked.connect(self.attach_requested)
        column.addWidget(self.overview_chat, alignment=Qt.AlignmentFlag.AlignHCenter)
        actions = QHBoxLayout()
        actions.addStretch()
        self.overview_open = QPushButton(self.tr('系统打开'))
        self.overview_open.setIcon(Icons.get_muted(Icons.EXTERNAL_OPEN))
        self.overview_open.clicked.connect(self.system_open_requested)
        actions.addWidget(self.overview_open)
        self.overview_reveal = QToolButton()
        self.overview_reveal.setIcon(Icons.get_muted(Icons.FOLDER))
        self.overview_reveal.setToolTip(self.tr('显示所在文件夹'))
        self.overview_reveal.setAccessibleName(self.overview_reveal.toolTip())
        self.overview_reveal.clicked.connect(self.reveal_requested)
        actions.addWidget(self.overview_reveal)
        actions.addStretch()
        column.addLayout(actions)
        column.addStretch()
        self.overview.setWidget(body)
        self.stack.addWidget(self.overview)

    def apply(self, preview, *, target=None):
        preserve_zoom = self.kind == preview.kind == "pdf" and self.page != preview.page
        self.kind, self.text = preview.kind, preview.text
        self.page, self.pages = preview.page, preview.pages
        self._image = preview.image
        self.source_visible = False
        self.raw.set_syntax('')
        self.raw.setPlainText(self.text)
        suffix = preview.document.path.suffix if preview.document is not None else {
            'markdown': '.md', 'html': '.html',
        }.get(self.kind, '')
        self.raw.set_syntax(suffix)
        self.raw.document().setModified(False)
        if getattr(preview, 'document', None) is not None:
            self.raw.set_wrapped(preview.document.longest_line < 2000)
        if self.kind == "markdown":
            self.markdown.set_markdown(self.text)
        else:
            self.markdown.set_markdown("")
        self._select_view()
        self.html.set_markup(preview.markup if self.kind == 'html' else '')
        self.picture.set_image(self._image, preserve_zoom=preserve_zoom)
        if self.kind == 'file' and target is not None:
            self.file_name.setText(target.name)
            self.file_path.setText(str(target.ref.locator or target.path or target.ref.ref))
            self.file_path.setToolTip(self.file_path.text())
            size = target.ref.size
            size_text = f'{size:,} B' if size < 1024 else (
                f'{size / 1024:.1f} KB' if size < 1024 ** 2 else f'{size / 1024 ** 2:.1f} MB')
            self.file_metadata.setText(f'{target.mime} · {size_text}')

    def set_source_visible(self, visible):
        self.source_visible = bool(visible)
        self._select_view()

    def set_wrapped(self, enabled):
        self.raw.set_wrapped(enabled)
        self.markdown.setLineWrapMode(self.markdown.LineWrapMode.WidgetWidth if enabled else self.markdown.LineWrapMode.NoWrap)
        self.markdown.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff if enabled else Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.html.setLineWrapMode(self.html.LineWrapMode.WidgetWidth if enabled else self.html.LineWrapMode.NoWrap)
        self.html.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff if enabled else Qt.ScrollBarPolicy.ScrollBarAsNeeded)

    def _select_view(self):
        self.stack.setCurrentIndex(4 if self.kind == 'file' else
                                   2 if self.kind in {"image", "pdf"} else
                                   3 if self.kind == 'html' and not self.source_visible else
                                   1 if self.kind == "markdown" and not self.source_visible else 0)

    @property
    def rich_view(self):
        return self.html if self.kind == 'html' else self.markdown

    @property
    def zoom_factor(self):
        return self.picture.zoom_factor

    def set_zoom(self, factor):
        self.picture.set_zoom(factor)

    def fit_image(self):
        self.picture.fit_image()

    def position(self):
        if self.kind == 'file':
            return 0
        return self.raw.verticalScrollBar().value() if self.stack.currentIndex() == 0 else self.rich_view.verticalScrollBar().value()

    def restore_position(self, position):
        if self.kind == 'file':
            return
        (self.raw.verticalScrollBar() if self.stack.currentIndex() == 0 else self.rich_view.verticalScrollBar()).setValue(position)

    def find_text(self, text, *, backwards=False):
        if self.stack.currentWidget() is self.raw:
            return self.raw.find_text(text, backwards=backwards)
        from PyQt6.QtGui import QTextDocument
        flags = QTextDocument.FindFlag.FindBackward if backwards else QTextDocument.FindFlag(0)
        if not text:
            return False
        if self.rich_view.find(text, flags):
            return True
        cursor = self.rich_view.textCursor()
        cursor.movePosition(cursor.MoveOperation.End if backwards else cursor.MoveOperation.Start)
        self.rich_view.setTextCursor(cursor)
        return self.rich_view.find(text, flags)
