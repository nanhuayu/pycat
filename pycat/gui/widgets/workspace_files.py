"""Workspace directory projection and native copy gestures; no filesystem I/O."""
from pathlib import PurePosixPath

from PyQt6.QtCore import QCoreApplication, QMimeData, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDrag
from PyQt6.QtWidgets import QApplication, QHBoxLayout, QLabel, QListWidgetItem, QToolButton, QVBoxLayout, QWidget

from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.theme import INSPECTOR_MARGIN, configure_icon_button
from pycat.gui.widgets.capsule import CapsuleDelegate, CapsuleList, SingleLineLabel
from pycat.models.workspace import WorkspaceLocation


class WorkspaceFileList(CapsuleList):
    files_dropped = pyqtSignal(list, object)
    drag_requested = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(self.SelectionMode.ExtendedSelection)
        self.setDragDropMode(self.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setAccessibleName(QCoreApplication.translate('WorkspaceFilesPanel', '工作区文件'))
        self.setToolTip(QCoreApplication.translate('WorkspaceFilesPanel', '单击预览 · Ctrl + 单击多选 · 拖入复制到工作区 · 拖出复制到本机'))
        self._prepared = ((), [])
        self.drag_started = False

    def selected_rows(self):
        return [item.data(Qt.ItemDataRole.UserRole) for item in self.selectedItems()]

    def clear_prepared(self):
        self._prepared = ((), [])

    def startDrag(self, supportedActions):
        self.drag_started = True
        rows = self.selected_rows()
        if rows and tuple(row["path"] for row in rows) == self._prepared[0]:
            self._drag_local(self._prepared[1])
        elif rows:
            self.drag_requested.emit(rows)

    def offer_drag(self, rows, paths):
        key = tuple(row["path"] for row in rows)
        self._prepared = (key, paths)
        if (key == tuple(row["path"] for row in self.selected_rows())
                and QApplication.mouseButtons() & Qt.MouseButton.LeftButton):
            self._drag_local(paths)
            return True
        return False

    def _drag_local(self, paths):
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)

    def mousePressEvent(self, event):
        self.drag_started = False
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        self.drag_started = False
        super().keyPressEvent(event)

    def mouseReleaseEvent(self, event):
        if self.drag_started:
            self.setState(self.State.NoState)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _accept_drop(self, event):
        urls = event.mimeData().urls()
        if (self.isEnabled() and event.source() is not self and urls
                and all(url.isLocalFile() for url in urls)
                and event.possibleActions() & Qt.DropAction.CopyAction):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            return True
        event.ignore()
        return False

    def dragEnterEvent(self, event):
        self._accept_drop(event)

    def dragMoveEvent(self, event):
        self._accept_drop(event)

    def dropEvent(self, event):
        if self._accept_drop(event):
            item = self.itemAt(event.position().toPoint())
            row = item.data(Qt.ItemDataRole.UserRole) if item else None
            self.files_dropped.emit([url.toLocalFile() for url in event.mimeData().urls()], row)


class WorkspaceFilesPanel(QWidget):
    refresh_requested = pyqtSignal(str)
    open_requested = pyqtSignal(dict)
    upload_requested = pyqtSignal(list, str)
    choose_upload_requested = pyqtSignal()
    download_requested = pyqtSignal(list)
    drag_requested = pyqtSignal(list)
    cancel_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.work_dir = ""
        self.directory = "."
        self.busy = self.loading = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(INSPECTOR_MARGIN, INSPECTOR_MARGIN, INSPECTOR_MARGIN, INSPECTOR_MARGIN)
        layout.setSpacing(4)
        self.workspace = SingleLineLabel()
        self.workspace.setProperty("muted", True)
        layout.addWidget(self.workspace)
        commands = QHBoxLayout()
        commands.setSpacing(4)
        self.up = self._button(QCoreApplication.translate('WorkspaceFilesPanel', '上一级'), Icons.ARROW_UP, lambda: self.refresh_requested.emit(PurePosixPath(self.directory).parent.as_posix()))
        commands.addWidget(self.up)
        self.path = SingleLineLabel("/")
        commands.addWidget(self.path, 1)
        self.refresh = self._button(QCoreApplication.translate('WorkspaceFilesPanel', '刷新文件'), Icons.REFRESH, lambda: self.refresh_requested.emit(self.directory))
        self.upload = self._button(QCoreApplication.translate('WorkspaceFilesPanel', '上传 / 复制文件到此目录'), Icons.UPLOAD, self.choose_upload_requested.emit)
        self.download = self._button(QCoreApplication.translate('WorkspaceFilesPanel', '下载 / 复制所选文件到本机'), Icons.DOWNLOAD, lambda: self.download_requested.emit(self.items.selected_rows()))
        for button in (self.refresh, self.upload, self.download):
            commands.addWidget(button)
        layout.addLayout(commands)
        progress = QHBoxLayout()
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setMinimumWidth(0)
        self.status.setProperty("muted", True)
        progress.addWidget(self.status, 1)
        self.cancel = self._button(QCoreApplication.translate('WorkspaceFilesPanel', '取消传输'), Icons.XMARK, self.cancel_requested.emit)
        progress.addWidget(self.cancel, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(progress)
        self.items = WorkspaceFileList()
        self.items.itemClicked.connect(self._open)
        self.items.itemActivated.connect(self._open)
        self.items.itemSelectionChanged.connect(self._sync_controls)
        self.items.files_dropped.connect(lambda paths, row: self.upload_requested.emit(
            paths, row["path"] if row and row["is_dir"] else self.directory))
        self.items.drag_requested.connect(self.drag_requested.emit)
        self.items.setMinimumWidth(0)
        layout.addWidget(self.items, 1)
        self.counter = SingleLineLabel()
        self.counter.setProperty("muted", True)
        layout.addWidget(self.counter)
        self.set_context("")

    def _button(self, label, icon, callback):
        button = QToolButton(self)
        configure_icon_button(button, Icons.get_muted(icon), label)
        button.clicked.connect(lambda: callback())
        return button

    def _open(self, item):
        if (item and not self.items.drag_started and len(self.items.selectedItems()) == 1
                and not QApplication.keyboardModifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)):
            self.open_requested.emit(item.data(Qt.ItemDataRole.UserRole))

    def set_context(self, work_dir):
        location = WorkspaceLocation.parse(work_dir)
        self.work_dir = location.value
        self.directory = "."
        self.busy = self.loading = False
        self.items.clear()
        self.items.clear_prepared()
        self.counter.clear()
        self.path.setText("/")
        self.workspace.setText(location.label)
        self.workspace.setToolTip(work_dir)
        self.set_status(QCoreApplication.translate('WorkspaceFilesPanel', '拖入文件或点击上传') if self.work_dir else QCoreApplication.translate('WorkspaceFilesPanel', '选择工作区后浏览文件'))
        self._sync_controls()

    def set_status(self, text):
        text = str(text or "")
        self.status.setText(text if len(text) <= 240 else text[:237] + "…")
        self.status.setToolTip(text)
        self.status.setVisible(bool(text))

    def _sync_controls(self):
        enabled = bool(self.work_dir) and not self.busy and not self.loading
        self.items.setEnabled(enabled)
        self.up.setEnabled(enabled and self.directory != ".")
        self.refresh.setEnabled(enabled)
        self.upload.setEnabled(enabled)
        rows = self.items.selected_rows()
        self.download.setEnabled(enabled and bool(rows) and all(not row["is_dir"] for row in rows))
        self.cancel.setVisible(self.busy)

    def set_busy(self, busy):
        self.busy = busy
        self._sync_controls()

    def set_loading(self, loading):
        self.loading = loading
        self._sync_controls()

    def apply(self, page):
        selected = {row["path"] for row in self.items.selected_rows()}
        scroll = self.items.verticalScrollBar().value()
        self.directory = page["path"]
        self.path.setText("/" if self.directory == "." else self.directory)
        self.items.clear()
        self.items.clear_prepared()
        for row in page["entries"]:
            item = QListWidgetItem(row["name"])
            item.setIcon(Icons.get_muted(Icons.FOLDER if row["is_dir"] else Icons.FILE))
            item.setData(Qt.ItemDataRole.UserRole, row)
            size = row["size"]
            if row["is_dir"]:
                detail = ""
            elif size >= 1024 * 1024:
                detail = f"{size / 1024 / 1024:.1f} MiB"
            elif size >= 1024:
                detail = f"{size / 1024:.1f} KiB"
            else:
                detail = f"{size} B"
            item.setData(CapsuleDelegate.DetailRole, detail)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, row["path"] + (QCoreApplication.translate('WorkspaceFilesPanel', ' · 文件夹') if row["is_dir"] else " · " + detail))
            item.setToolTip(row["path"])
            self.items.addItem(item)
            item.setSelected(row["path"] in selected)
        self.items.verticalScrollBar().setValue(scroll)
        self.counter.setText(QCoreApplication.translate('WorkspaceFilesPanel', '{value} 项').format(value=len(page['entries'])) + (QCoreApplication.translate('WorkspaceFilesPanel', ' · 仅显示部分内容') if page["truncated"] else ""))
        self.set_status("" if page["entries"] else QCoreApplication.translate('WorkspaceFilesPanel', '此目录为空，可拖入文件'))
        self.set_loading(False)

    def showEvent(self, event):
        super().showEvent(event)
        if self.work_dir and not self.busy:
            self.refresh_requested.emit(self.directory)
