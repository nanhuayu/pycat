"""One display body for Markdown, plain text, images and PDF pages."""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPixmap, QPainter
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QScrollArea, QStackedWidget, QGraphicsView, QGraphicsScene
from pycat.gui.widgets.markdown_view import MarkdownView
from pycat.gui.widgets.themed_line_edit import ThemedPlainTextEdit


class ImageCanvas(QGraphicsView):
    """Transform one bounded raster in Qt; dragging never re-reads the file."""
    zoom_changed = pyqtSignal(float)

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

    def set_image(self, image, *, preserve_zoom=False):
        self.scene().clear()
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


class ContentViewer(QWidget):
    zoom_changed = pyqtSignal(float)
    def __init__(self, parent=None):
        super().__init__(parent)
        self.kind, self.text, self._image = "", "", None
        self.page, self.pages = 1, 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.source_visible = False
        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.raw = ThemedPlainTextEdit()
        self.raw.setObjectName("content_text_preview")
        self.raw.setReadOnly(True)
        self.stack.addWidget(self.raw)
        self.markdown = MarkdownView()
        self.markdown.setObjectName("content_preview_surface")
        self.markdown.setProperty("theme", "light")
        self.markdown.document().setDocumentMargin(10)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setObjectName("content_preview_scroll")
        self.scroll.viewport().setObjectName("content_preview_viewport")
        self.scroll.setWidget(self.markdown)
        self.stack.addWidget(self.scroll)
        self.picture = ImageCanvas()
        self.picture.setMinimumSize(1, 1)
        self.picture.setObjectName("image_viewer_surface")
        self.picture.zoom_changed.connect(self.zoom_changed)
        self.stack.addWidget(self.picture)

    def apply(self, preview):
        preserve_zoom = self.kind == preview.kind == "pdf" and self.page != preview.page
        self.kind, self.text = preview.kind, preview.text
        self.page, self.pages = preview.page, preview.pages
        self._image = preview.image
        self.source_visible = False
        self.raw.setPlainText(self.text)
        if self.kind == "markdown":
            self.markdown.set_markdown(self.text)
        else:
            self.markdown.set_markdown("")
        self._select_view()
        self.picture.set_image(self._image, preserve_zoom=preserve_zoom)

    def set_source_visible(self, visible):
        self.source_visible = bool(visible)
        self._select_view()

    def _select_view(self):
        self.stack.setCurrentIndex(2 if self.kind in {"image", "pdf"} else
                                   1 if self.kind == "markdown" and not self.source_visible else 0)

    @property
    def zoom_factor(self):
        return self.picture.zoom_factor

    def set_zoom(self, factor):
        self.picture.set_zoom(factor)

    def fit_image(self):
        self.picture.fit_image()

    def position(self):
        return self.raw.verticalScrollBar().value() if self.stack.currentIndex() == 0 else self.scroll.verticalScrollBar().value()

    def restore_position(self, position):
        (self.raw.verticalScrollBar() if self.stack.currentIndex() == 0 else self.scroll.verticalScrollBar()).setValue(position)
