"""Small, independent screen selection, review and floating-image widgets."""
from __future__ import annotations

import base64
import threading
from datetime import datetime

from PyQt6.QtCore import QBuffer, QIODevice, QPointF, QRect, QRectF, QSignalBlocker, QSize, Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import QColor, QGuiApplication, QImage, QKeySequence, QPainter, QPainterPath, QPen, QShortcut
from PyQt6.QtWidgets import (
    QAbstractSpinBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSpinBox,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pycat.gui.runtime.background_job import BackgroundJob
from pycat.gui.runtime.screen_capture import crop_capture
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.theme import configure_icon_button, prepare_context_menu
from pycat.gui.utils.window_geometry import apply_window_size
from pycat.gui.widgets.content_viewer import ImageCanvas
from pycat.gui.widgets.themed_line_edit import ThemedPlainTextEdit


class CaptureOverlay(QWidget):
    selected = pyqtSignal(QImage)
    cancelled = pyqtSignal()

    def __init__(self, image: QImage, geometry: QRect, parent=None):
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.image = image
        self._origin: QPointF | None = None
        self._selection = QRectF()
        self.setGeometry(geometry)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("拖动框选截图，Enter 确认，Esc 取消")
        self._confirmed = False
        self.toolbar = QFrame(self)
        self.toolbar.setObjectName('capture_toolbar')
        self.toolbar.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QHBoxLayout(self.toolbar)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)
        self._region_fields = []
        for key, label, description in (
            ('x', 'X', '横坐标'), ('y', 'Y', '纵坐标'), ('width', 'W', '宽度'), ('height', 'H', '高度'),
        ):
            spin = QSpinBox()
            spin.setFixedWidth(60)
            spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
            spin.setKeyboardTracking(False)
            spin.setAlignment(Qt.AlignmentFlag.AlignRight)
            spin.setAccessibleName('截图区域' + description)
            spin.setToolTip(description + ' · 相对当前屏幕的逻辑像素')
            spin.valueChanged.connect(self._apply_precision)
            setattr(self, key + '_spin', spin)
            self._region_fields.append(spin)
            layout.addWidget(QLabel(label))
            layout.addWidget(spin)
        layout.addSpacing(2)
        for name, icon, label in (
            ('confirm', Icons.CHECK, '确认截图 · Enter'),
            ('cancel', Icons.XMARK, '取消截图 · Esc'),
        ):
            button = QToolButton()
            configure_icon_button(button, Icons.get_muted(icon), label)
            layout.addWidget(button)
            setattr(self, name + '_button', button)
        self.confirm_button.clicked.connect(self._confirm)
        self.cancel_button.clicked.connect(self.cancelled.emit)
        self.toolbar.hide()
        for key in ('Return', 'Enter'):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(self._confirm)
        QShortcut(QKeySequence('Esc'), self).activated.connect(self.cancelled.emit)

    def _sync_selection_controls(self):
        rect = self._selection.toRect()
        values = {'x': rect.x(), 'y': rect.y(), 'width': rect.width(), 'height': rect.height()}
        maxima = {'x': self.width() - 1, 'y': self.height() - 1,
                  'width': self.width() - rect.x(), 'height': self.height() - rect.y()}
        for key, value in values.items():
            spin = getattr(self, key + '_spin')
            with QSignalBlocker(spin):
                spin.setRange(1 if key in ('width', 'height') else 0, maxima[key])
                spin.setValue(value)
        self.update()

    def _apply_precision(self):
        x, y = self.x_spin.value(), self.y_spin.value()
        width = min(self.width_spin.value(), self.width() - x)
        height = min(self.height_spin.value(), self.height() - y)
        self._selection = QRectF(x, y, width, height)
        self._sync_selection_controls()

    def _position_toolbar(self):
        self.toolbar.adjustSize()
        x = self._selection.right() - self.toolbar.width()
        y = self._selection.bottom() + 8
        if y + self.toolbar.height() > self.height() - 8:
            y = self._selection.top() - self.toolbar.height() - 8
        x = max(0, min(int(x), self.width() - self.toolbar.width() - 8))
        y = max(0, min(int(y), self.height() - self.toolbar.height() - 8))
        self.toolbar.move(x, y)

    def _confirm(self):
        if self._selection.isEmpty() or self._confirmed:
            return
        for spin in self._region_fields:
            spin.interpretText()
        self._confirmed = True
        self.selected.emit(crop_capture(self.image, self.rect(), self._selection))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.drawImage(QRectF(self.rect()), self.image)
        mask = QPainterPath()
        mask.addRect(QRectF(self.rect()))
        mask.addRect(self._selection)
        painter.fillPath(mask, QColor(0, 0, 0, 105))
        painter.setPen(QPen(self.palette().highlight().color(), 2))
        if not self._selection.isEmpty():
            painter.drawRect(self._selection)
        painter.setPen(QColor('white'))
        painter.drawText(20, 30, "拖动框选 · Enter 确认 · Esc 取消")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self.cancelled.emit()
        elif event.button() == Qt.MouseButton.LeftButton:
            self.toolbar.hide()
            self._origin = event.position()
            self._selection = QRectF()
            self.update()

    def mouseMoveEvent(self, event):
        if self._origin is not None:
            self._selection = QRectF(self._origin, event.position()).normalized().intersected(QRectF(self.rect()))
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self._origin is None:
            return
        rect = QRectF(self._origin, event.position()).normalized().intersected(QRectF(self.rect()))
        self._origin = None
        if min(rect.width(), rect.height()) < 4:
            return
        self._selection = QRectF(rect.toAlignedRect().intersected(self.rect()))
        self._sync_selection_controls()
        self.toolbar.show()
        self._position_toolbar()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._confirm()
        else:
            super().keyPressEvent(event)


class ScreenshotDialog(QDialog):
    attach_requested = pyqtSignal(str)
    pin_requested = pyqtSignal(QImage)

    def __init__(self, image: QImage, *, ocr_service, parent=None):
        super().__init__(parent)
        self.image = image
        self._ocr_service = ocr_service
        self._job: BackgroundJob | None = None
        self._cancel = threading.Event()
        self.setWindowTitle("截图")
        apply_window_size(self, preferred=(780, 540), minimum=(360, 300))
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        bar = QHBoxLayout()
        bar.setSpacing(4)
        for name, label, icon, callback in (
            ('copy', '复制图片', Icons.COPY, self.copy), ('attach', '加入对话', Icons.PAPERCLIP, self.attach),
            ('save', '保存图片', Icons.DOWNLOAD, self.save), ('pin', '贴图', Icons.PIN, lambda: self.pin_requested.emit(self.image)),
            ('ocr', '提取文字', Icons.OCR, self.extract_text),
        ):
            button = QToolButton()
            configure_icon_button(button, Icons.get_muted(icon), label)
            button.clicked.connect(callback)
            setattr(self, name + '_button', button)
            bar.addWidget(button)
        bar.addStretch()
        fit = self.fit_button = QToolButton()
        configure_icon_button(fit, Icons.get_muted(Icons.FIT_IMAGE), '适应窗口')
        bar.addWidget(fit)
        root.addLayout(bar)
        self.tabs = QTabWidget()
        self.canvas = ImageCanvas()
        self.canvas.setObjectName('image_viewer_surface')
        self.canvas.set_image(image)
        self.tabs.addTab(self.canvas, '图片')
        self.tabs.tabBar().hide()
        self.text = ThemedPlainTextEdit(self.tabs)
        self.text.setReadOnly(True)
        self.text.hide()
        self.tabs.currentChanged.connect(self._sync_commands)
        root.addWidget(self.tabs, 1)
        fit.clicked.connect(self.canvas.fit_image)
        self.notice = QLabel(f'{image.width()} × {image.height()} · 拖动平移，Ctrl + 滚轮缩放')
        self.notice.setProperty('muted', True)
        self.notice.setWordWrap(True)
        root.addWidget(self.notice)
        self.finished.connect(self._dispose)

    def _sync_commands(self):
        reading_text = self.tabs.currentWidget() is self.text
        label = '复制文字' if reading_text else '复制图片'
        self.copy_button.setText(label)
        self.copy_button.setToolTip(label)
        self.copy_button.setAccessibleName(label)
        self.fit_button.setEnabled(not reading_text)

    def png_bytes(self):
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not self.image.save(buffer, 'PNG'):
            raise ValueError('无法编码截图')
        return bytes(buffer.data())

    def copy(self):
        clipboard = QGuiApplication.clipboard()
        if self.tabs.currentWidget() is self.text:
            clipboard.setText(self.text.toPlainText())
            self.notice.setText('已复制文字')
        else:
            clipboard.setImage(self.image)
            self.notice.setText('已复制图片，可粘贴到对话或其他应用')

    def attach(self):
        self.attach_requested.emit('data:image/png;base64,' + base64.b64encode(self.png_bytes()).decode('ascii'))

    def save(self):
        path, _ = QFileDialog.getSaveFileName(self, '保存截图', datetime.now().strftime('截图-%Y%m%d-%H%M%S.png'), 'PNG 图片 (*.png)')
        if path:
            self.notice.setText('已保存截图' if self.image.save(path, 'PNG') else '保存失败，请检查目标位置')

    def extract_text(self):
        if self._job is not None:
            return
        self.notice.setText('正在提取文字…')
        self.ocr_button.setEnabled(False)
        self._cancel.clear()
        data, service, cancel = self.png_bytes(), self._ocr_service, self._cancel
        self._job = BackgroundJob(lambda: service.recognize_capture(data, cancel_event=cancel))
        self._job.signals.finished.connect(self._ocr_finished)
        QThreadPool.globalInstance().start(self._job)

    def _ocr_finished(self, result, error):
        self._job = None
        if self._cancel.is_set():
            return
        self.ocr_button.setEnabled(True)
        if error:
            self.notice.setText(str(error))
            return
        if self.tabs.indexOf(self.text) < 0:
            self.tabs.addTab(self.text, '文字')
        self.tabs.tabBar().show()
        self.text.setPlainText(result or '')
        self.tabs.setCurrentWidget(self.text)
        self.notice.setText('提取完成，可选择或复制文字' if result else '未识别到文字，可重新截取更清晰的区域')

    def _dispose(self):
        self._cancel.set()
        if self._job is not None:
            self._job.abandon()
            self._job = None


class PinnedImage(QWidget):
    """A transient image on the desktop; drag to move, Escape/double click to close."""
    def __init__(self, image: QImage, parent=None):
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.image = image
        self._drag = None
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(image.size().scaled(QSize(640, 480), Qt.AspectRatioMode.KeepAspectRatio))
        self.setToolTip('拖动移动 · 双击或 Esc 关闭 · Ctrl+C 复制')
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(self.rect(), self.image)
        painter.setPen(self.palette().mid().color())
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event):
        if self._drag is not None:
            self.move(event.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, event):
        self._drag = None

    def mouseDoubleClickEvent(self, event):
        self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        elif event.matches(QKeySequence.StandardKey.Copy):
            QGuiApplication.clipboard().setImage(self.image)
        else:
            super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        menu = prepare_context_menu(QMenu(self), self)
        menu.addAction('复制图片', lambda: QGuiApplication.clipboard().setImage(self.image))
        menu.addAction('关闭贴图', self.close)
        menu.exec(event.globalPos())
