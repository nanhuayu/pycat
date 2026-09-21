"""One nonmodal window projecting session-owned process handles."""
from collections import OrderedDict

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget

from pycat.gui.widgets.terminal_view import TerminalView
from pycat.gui.widgets.capsule import SingleLineLabel


class ShellWindow(QDialog):
    new_requested = pyqtSignal()
    control_requested = pyqtSignal(str, str)
    input_requested = pyqtSignal(str, str)
    response_requested = pyqtSignal(str, str)
    resize_requested = pyqtSignal(str, int, int)
    stop_requested = pyqtSignal(str)
    selected = pyqtSignal()
    view_discarded = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setObjectName("shell_window")
        self.setWindowTitle("Shell")
        self.setModal(False)
        self.resize(960, 620)
        self.setMinimumSize(580, 360)
        self.conversation_id = ""
        self.snapshots = {}
        self._pages = {}
        self._views = OrderedDict()
        self._last_selected = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        title_row = QHBoxLayout()
        self.heading = SingleLineLabel("Shell")
        font = self.heading.font()
        font.setBold(True)
        self.heading.setFont(font)
        title_row.addWidget(self.heading, 1)
        self.new_button = QPushButton("＋ 新建 Shell")
        self.new_button.clicked.connect(self.new_requested.emit)
        title_row.addWidget(self.new_button)
        layout.addLayout(title_row)
        self.scope_label = SingleLineLabel()
        self.scope_label.setProperty("muted", True)
        layout.addWidget(self.scope_label)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.currentChanged.connect(self._select)
        layout.addWidget(self.tabs, 1)
        self.empty_label = QLabel("暂无 Shell\n新建交互 Shell，或在对话中运行命令后查看输出。")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_label, 1)
        actions = QHBoxLayout()
        self.state_label = QLabel()
        self.state_label.setWordWrap(True)
        actions.addWidget(self.state_label, 1)
        self.control_button = QPushButton("接管输入")
        self.control_button.clicked.connect(self._control)
        actions.addWidget(self.control_button)
        self.interrupt_button = QPushButton("Ctrl+C")
        self.interrupt_button.setToolTip("中断前台程序，保留 Shell")
        self.interrupt_button.clicked.connect(lambda: self.input_requested.emit(self.process_id, "\x03"))
        actions.addWidget(self.interrupt_button)
        self.stop_button = QPushButton("结束")
        self.stop_button.setToolTip("结束此进程及其子进程")
        self.stop_button.clicked.connect(lambda: self.stop_requested.emit(self.process_id))
        actions.addWidget(self.stop_button)
        layout.addLayout(actions)
        self.notice = QLabel("关闭窗口会隐藏终端，运行中的 Shell 会继续。")
        self.notice.setProperty("muted", True)
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self._select()

    @property
    def process_id(self):
        page = self.tabs.currentWidget()
        return str(page.property("process_id") or "") if page else ""

    @property
    def view(self):
        return self._views.get((self.conversation_id, self.process_id))

    def set_scope(self, conversation_id, title, work_dir):
        if conversation_id != self.conversation_id:
            self.tabs.blockSignals(True)
            while self.tabs.count():
                page = self.tabs.widget(0)
                self.tabs.removeTab(0)
                page.hide()
            self.tabs.blockSignals(False)
            self.snapshots = {}
            for key in tuple(self._pages):
                if key not in self._views:
                    self._pages.pop(key).deleteLater()
        self.conversation_id = conversation_id
        self.heading.setText(f"Shell · {title or '新会话'}")
        self.scope_label.setText(f"{work_dir or '未设置工作目录'}  ·  固定在此会话")
        self._select()

    def update_processes(self, snapshots, selected_id=""):
        self.snapshots = {s.process_id: s for s in snapshots}
        self.tabs.blockSignals(True)
        current = self.process_id
        for key in tuple(self._pages):
            if key[0] == self.conversation_id and key[1] not in self.snapshots:
                self._discard_view(key)
                page = self._pages.pop(key)
                self.tabs.removeTab(self.tabs.indexOf(page))
                page.deleteLater()
        for snapshot in snapshots:
            key = (self.conversation_id, snapshot.process_id)
            page = self._pages.get(key)
            if page is None:
                page = QWidget()
                page.setProperty("process_id", snapshot.process_id)
                self._pages[key] = page
                content = QVBoxLayout(page)
                content.setContentsMargins(0, 0, 0, 0)
            index = self.tabs.indexOf(page)
            label = f"{'●' if snapshot.running else '○'} {snapshot.backend} · {snapshot.process_id[:6]}"
            if index < 0:
                index = self.tabs.addTab(page, label)
            self.tabs.setTabText(index, label)
            self.tabs.setTabToolTip(index, f"{snapshot.command}\n{snapshot.cwd}")
        target = selected_id or current
        page = self._pages.get((self.conversation_id, target))
        if page:
            self.tabs.setCurrentWidget(page)
        self.tabs.blockSignals(False)
        self._select()

    def _select(self, *_args):
        snapshot = self.snapshots.get(self.process_id)
        self.tabs.setVisible(bool(self.snapshots))
        self.empty_label.setVisible(not self.snapshots)
        running = bool(snapshot and snapshot.running)
        interactive = bool(snapshot and snapshot.interactive)
        user = bool(snapshot and snapshot.controller == "user")
        self.control_button.setVisible(interactive)
        self.control_button.setEnabled(running)
        self.control_button.setText("归还 Agent" if user else "接管输入")
        self.interrupt_button.setVisible(interactive)
        self.interrupt_button.setEnabled(running and user)
        self.stop_button.setEnabled(running)
        if snapshot:
            status = "状态未确认" if snapshot.error else ("运行中" if running else f"已退出 · {snapshot.exit_code}")
            self.state_label.setText(f"{status}  ·  {'由你输入' if user else 'Agent 控制'}" if interactive else f"{status}  ·  日志")
            key = (self.conversation_id, self.process_id)
            if key not in self._views:
                view = TerminalView() if interactive else QPlainTextEdit()
                if interactive:
                    view.input_ready.connect(lambda text, key=key: self._emit_scoped(self.input_requested, key, text))
                    view.response_ready.connect(lambda text, key=key: self._emit_scoped(self.response_requested, key, text))
                    view.dimensions_changed.connect(lambda cols, rows, key=key: self._emit_scoped(self.resize_requested, key, cols, rows))
                else:
                    view.setReadOnly(True)
                    view.setMaximumBlockCount(5000)
                self._views[key] = view
                self._pages[key].layout().addWidget(view)
            self._views.move_to_end(key)
            while len(self._views) > 16:
                self._discard_view(next(iter(self._views)))
            if interactive:
                self._views[key].set_input_enabled(running and user)
                if key != self._last_selected and user and self.isActiveWindow():
                    self._views[key].setFocus()
            self._last_selected = key
        else:
            self.state_label.setText("")
            self._last_selected = None
        self.selected.emit()

    def _discard_view(self, key):
        view = self._views.pop(key, None)
        if view is not None:
            self._pages[key].layout().removeWidget(view)
            view.deleteLater()
            self.view_discarded.emit(*key)
        if key[0] != self.conversation_id:
            self._pages.pop(key).deleteLater()

    def cached_view(self, scope, process_id):
        return self._views.get((scope, process_id))

    @staticmethod
    def append_log(view, text):
        bar = view.verticalScrollBar()
        bottom = bar.value() == bar.maximum()
        cursor = view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        # A single unbroken output line bypasses maximumBlockCount.
        excess = view.document().characterCount() - 1 - 262144
        if excess > 0:
            cursor.setPosition(0)
            cursor.setPosition(excess, QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
        if bottom:
            bar.setValue(bar.maximum())

    def _emit_scoped(self, signal, key, *args):
        if key == (self.conversation_id, self.process_id):
            signal.emit(key[1], *args)

    def _control(self):
        snapshot = self.snapshots.get(self.process_id)
        if snapshot:
            self.control_requested.emit(snapshot.process_id, "agent" if snapshot.controller == "user" else "user")

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def reject(self):
        self.hide()

    def dispose(self):
        for page in self._pages.values():
            page.deleteLater()
        self._pages.clear()
        self._views.clear()
        self.hide()
        self.deleteLater()
