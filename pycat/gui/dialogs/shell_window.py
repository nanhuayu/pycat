"""One nonmodal window projecting session-owned process handles."""
from collections import OrderedDict

from PyQt6.QtCore import QCoreApplication, Qt, pyqtSignal
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pycat.gui.utils.icon_manager import Icons
from pycat.gui.widgets.capsule import SingleLineLabel
from pycat.gui.widgets.terminal_view import TerminalView
from pycat.gui.widgets.themed_line_edit import ThemedPlainTextEdit


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
        super().__init__(None, Qt.WindowType.Window)
        self.setObjectName("shell_window")
        self.setWindowTitle("Shell")
        self.setWindowIcon(Icons.brand())
        self.setModal(False)
        self.resize(960, 620)
        self.setMinimumSize(580, 360)
        self.conversation_id = ""
        self.snapshots = {}
        self._pages = {}
        self._views = OrderedDict()
        self._last_selected = None
        self._hidden = set()
        self._disposed = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        title_row = QHBoxLayout()
        self.heading = SingleLineLabel("Shell")
        font = self.heading.font()
        font.setBold(True)
        self.heading.setFont(font)
        title_row.addWidget(self.heading, 1)
        self.new_button = QPushButton(QCoreApplication.translate('ShellWindow', '新建 Shell'))
        self.new_button.setIcon(Icons.get(Icons.PLUS))
        self.new_button.clicked.connect(self.new_requested.emit)
        title_row.addWidget(self.new_button)
        layout.addLayout(title_row)
        self.scope_label = SingleLineLabel()
        self.scope_label.setProperty("muted", True)
        layout.addWidget(self.scope_label)
        self.body = QSplitter()
        navigation = QWidget()
        nav_layout = QVBoxLayout(navigation)
        nav_layout.setContentsMargins(0, 0, 8, 0)
        nav_actions = QHBoxLayout()
        self.history_button = QPushButton(self.tr('历史'))
        self.history_button.setCheckable(True)
        self.history_button.setToolTip(self.tr('显示此会话的全部进程记录'))
        self.history_button.toggled.connect(self._toggle_history)
        nav_actions.addWidget(self.history_button)
        nav_actions.addStretch()
        self.close_button = QToolButton()
        self.close_button.setIcon(Icons.get_muted(Icons.XMARK))
        self.close_button.setToolTip(self.tr('关闭显示，保留进程和日志'))
        self.close_button.setAccessibleName(self.close_button.toolTip())
        self.close_button.clicked.connect(lambda: self.close_process(self.process_id))
        nav_actions.addWidget(self.close_button)
        nav_layout.addLayout(nav_actions)
        self.process_list = QListWidget()
        self.process_list.setObjectName('shell_process_list')
        self.process_list.setSpacing(2)
        self.process_list.currentItemChanged.connect(self._select)
        nav_layout.addWidget(self.process_list, 1)
        self.body.addWidget(navigation)
        self.pages = QStackedWidget()
        self.body.addWidget(self.pages)
        self.body.setStretchFactor(1, 1)
        self.body.setSizes([190, 720])
        layout.addWidget(self.body, 1)
        self.empty_label = QLabel(QCoreApplication.translate('ShellWindow', '暂无 Shell\n新建交互 Shell，或在对话中运行命令后查看输出。'))
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.pages.addWidget(self.empty_label)
        actions = QHBoxLayout()
        self.state_label = QLabel()
        self.state_label.setWordWrap(True)
        actions.addWidget(self.state_label, 1)
        self.control_button = QPushButton(QCoreApplication.translate('ShellWindow', '接管输入'))
        self.control_button.clicked.connect(self._control)
        actions.addWidget(self.control_button)
        self.interrupt_button = QPushButton("Ctrl+C")
        self.interrupt_button.setToolTip(QCoreApplication.translate('ShellWindow', '中断前台程序，保留 Shell'))
        self.interrupt_button.clicked.connect(lambda: self.input_requested.emit(self.process_id, "\x03"))
        actions.addWidget(self.interrupt_button)
        self.stop_button = QPushButton(QCoreApplication.translate('ShellWindow', '结束'))
        self.stop_button.setToolTip(QCoreApplication.translate('ShellWindow', '结束此进程及其子进程'))
        self.stop_button.clicked.connect(lambda: self.stop_requested.emit(self.process_id))
        actions.addWidget(self.stop_button)
        layout.addLayout(actions)
        self.notice = QLabel(QCoreApplication.translate('ShellWindow', '关闭窗口会隐藏终端，运行中的 Shell 会继续。'))
        self.notice.setProperty("muted", True)
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self._select()

    @property
    def process_id(self):
        item = self.process_list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""

    @property
    def view(self):
        return self._views.get((self.conversation_id, self.process_id))

    def set_scope(self, conversation_id, title, work_dir):
        if conversation_id != self.conversation_id:
            self.process_list.blockSignals(True)
            self.process_list.clear()
            for index in reversed(range(self.pages.count())):
                page = self.pages.widget(index)
                if page is not self.empty_label:
                    self.pages.removeWidget(page)
                    page.hide()
            self.process_list.blockSignals(False)
            self.history_button.blockSignals(True)
            self.history_button.setChecked(False)
            self.history_button.blockSignals(False)
            self.snapshots = {}
            for key in tuple(self._pages):
                if key not in self._views:
                    self._pages.pop(key).deleteLater()
        self.conversation_id = conversation_id
        self.heading.setText(QCoreApplication.translate('ShellWindow', 'Shell · {value}').format(value=title or QCoreApplication.translate('ShellWindow', '新会话')))
        self.scope_label.setText(QCoreApplication.translate('ShellWindow', '{value}  ·  固定在此会话').format(value=work_dir or QCoreApplication.translate('ShellWindow', '未设置工作目录')))
        self._select()

    def update_processes(self, snapshots, selected_id=""):
        self.snapshots = {s.process_id: s for s in snapshots}
        current = self.process_id
        target = selected_id or current
        if selected_id:
            self._hidden.discard((self.conversation_id, selected_id))
        # Core snapshots remain authoritative. This is only a window-local
        # navigation filter; hidden records can be reopened through history.
        visible = [s for s in snapshots if (self.conversation_id, s.process_id) not in self._hidden]
        active = [s for s in visible if s.running]
        ended = sorted((s for s in visible if not s.running),
                       key=lambda s: getattr(s, 'started_at', 0), reverse=True)
        if self.history_button.isChecked():
            visible.sort(key=lambda s: (not s.running, -getattr(s, 'started_at', 0)))
        else:
            visible = active + ended[:8]
            selected = self.snapshots.get(target)
            if selected and selected in ended and selected not in visible:
                visible.append(selected)
        self.process_list.blockSignals(True)
        scroll = self.process_list.verticalScrollBar().value()
        self.process_list.clear()
        for key in tuple(self._pages):
            if key[0] == self.conversation_id and key[1] not in self.snapshots:
                self._discard_view(key)
                page = self._pages.pop(key)
                self.pages.removeWidget(page)
                page.deleteLater()
        visible_keys = {(self.conversation_id, s.process_id) for s in visible}
        for key, page in tuple(self._pages.items()):
            if key[0] == self.conversation_id and key not in visible_keys:
                self.pages.removeWidget(page)
                page.hide()
                if key not in self._views:
                    self._pages.pop(key).deleteLater()
        for snapshot in visible:
            key = (self.conversation_id, snapshot.process_id)
            page = self._pages.get(key)
            if page is None:
                page = QWidget()
                page.setProperty("process_id", snapshot.process_id)
                self._pages[key] = page
                content = QVBoxLayout(page)
                content.setContentsMargins(0, 0, 0, 0)
            if self.pages.indexOf(page) < 0:
                self.pages.addWidget(page)
            label = f"{'●' if snapshot.running else '○'} {snapshot.backend} · {snapshot.process_id[:6]}"
            item = QListWidgetItem(Icons.get_muted(Icons.TERMINAL), label)
            item.setData(Qt.ItemDataRole.UserRole, snapshot.process_id)
            item.setToolTip(f"{snapshot.command}\n{snapshot.cwd}")
            self.process_list.addItem(item)
            if snapshot.process_id == target:
                self.process_list.setCurrentItem(item)
        if self.process_list.currentItem() is None and self.process_list.count():
            self.process_list.setCurrentRow(0)
        self.process_list.verticalScrollBar().setValue(scroll)
        self.process_list.blockSignals(False)
        self._select()

    def close_process(self, process_id):
        if not process_id:
            return
        key = (self.conversation_id, process_id)
        self._hidden.add(key)
        self._discard_view(key)
        self.update_processes(list(self.snapshots.values()))

    def _toggle_history(self, enabled):
        if enabled:
            self._hidden.difference_update((self.conversation_id, pid) for pid in self.snapshots)
        self.update_processes(list(self.snapshots.values()))

    def _select(self, *_args):
        snapshot = self.snapshots.get(self.process_id)
        self.close_button.setEnabled(snapshot is not None)
        running = bool(snapshot and snapshot.running)
        interactive = bool(snapshot and snapshot.interactive)
        user = bool(snapshot and snapshot.controller == "user")
        self.control_button.setVisible(interactive)
        self.control_button.setEnabled(running)
        self.control_button.setText(QCoreApplication.translate('ShellWindow', '归还 Agent') if user else QCoreApplication.translate('ShellWindow', '接管输入'))
        self.interrupt_button.setVisible(interactive)
        self.interrupt_button.setEnabled(running and user)
        self.stop_button.setEnabled(running)
        if snapshot:
            status = QCoreApplication.translate('ShellWindow', '状态未确认') if snapshot.error else (QCoreApplication.translate('ShellWindow', '运行中') if running else QCoreApplication.translate('ShellWindow', '已退出 · {exit_code}').format(exit_code=snapshot.exit_code))
            self.state_label.setText(QCoreApplication.translate('ShellWindow', '{status}  ·  {value}').format(status=status, value=QCoreApplication.translate('ShellWindow', '由你输入') if user else QCoreApplication.translate('ShellWindow', 'Agent 控制')) if interactive else QCoreApplication.translate('ShellWindow', '{status}  ·  日志').format(status=status))
            key = (self.conversation_id, self.process_id)
            self.pages.setCurrentWidget(self._pages[key])
            if key not in self._views:
                view = TerminalView() if interactive else ThemedPlainTextEdit()
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
            self.pages.setCurrentWidget(self.empty_label)
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
        if self._disposed:
            return
        self._disposed = True
        for page in self._pages.values():
            page.deleteLater()
        self._pages.clear()
        self._views.clear()
        self.hide()
        self.deleteLater()
