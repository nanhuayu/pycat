"""Choose from existing conversation summaries; no separate workspace store."""
import os
import threading
import time

from PyQt6.QtCore import Qt, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QFileDialog, QTabWidget, QWidget, QLineEdit

from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.window_geometry import apply_window_size
from pycat.gui.widgets.themed_line_edit import ThemedLineEdit
from pycat.gui.runtime.background_job import BackgroundJob
from pycat.models.workspace import WorkspaceLocation, workspace_identity


class WorkspacePickerDialog(QDialog):
    auth_requested = pyqtSignal(object)

    def __init__(self, summaries, *, current="", parent=None, workspace_service=None):
        super().__init__(parent)
        self._workspaces = workspace_service
        self._job = None
        self._cancel = threading.Event()
        self.auth_requested.connect(self._authenticate)
        self.selected_path = ""
        self.current = current
        self.setWindowTitle("选择工作区")
        apply_window_size(self, preferred=(520, 430), minimum=(360, 280))
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        self.tabs = QTabWidget()
        local = QWidget()
        local_layout = QVBoxLayout(local)
        local_layout.setContentsMargins(0, 8, 0, 0)
        self.tabs.addTab(local, "最近与本地")
        root.addWidget(self.tabs, 1)
        self.search = ThemedLineEdit()
        self.search.setPlaceholderText("搜索最近工作区")
        self.search.setClearButtonEnabled(True)
        local_layout.addWidget(self.search)
        self.recent_list = QListWidget()
        self.recent_list.setAccessibleName("最近工作区")
        self.recent_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.recent_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        rows = sorted(summaries, key=lambda row: str(row.get("updated_at") or ""), reverse=True)
        if current:
            rows.insert(0, {"work_dir": current})
        seen = set()
        for row in rows:
            path = str(row.get("work_dir") or "").strip()
            location = WorkspaceLocation.parse(path)
            key = workspace_identity(path) if path else ""
            if not key or key in seen:
                continue
            seen.add(key)
            display_path = f"{location.endpoint}:{location.root}" if location.is_remote else path
            item = QListWidgetItem(Icons.get_muted(Icons.FOLDER), f"{location.label or path}\n{display_path}")
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(display_path)
            self.recent_list.addItem(item)
        local_layout.addWidget(self.recent_list, 1)
        self._build_ssh_tab()
        self.notice = QLabel("暂无最近工作区，请浏览文件夹。" if not seen else "")
        self.notice.setObjectName("workspace_notice")
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setWordWrap(True)
        self.notice.setProperty("muted", True)
        root.addWidget(self.notice)
        actions = QHBoxLayout()
        self.browse = QPushButton("浏览文件夹…")
        self.browse.setIcon(Icons.get_muted(Icons.FOLDER))
        self.browse.clicked.connect(self._browse)
        self.tabs.currentChanged.connect(lambda index: self.browse.setVisible(index == 0))
        actions.addWidget(self.browse)
        actions.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        root.addLayout(actions)
        self.search.textChanged.connect(self._filter)
        self.recent_list.itemClicked.connect(self._select)
        self.recent_list.itemActivated.connect(self._select)
        self.search.returnPressed.connect(self._select_first)
        self.search.setFocus()
        self.finished.connect(self._finished)
        current_location = WorkspaceLocation.parse(current)
        if current_location.is_remote:
            self.ssh_host.setText(current_location.host)
            self.ssh_port.setText(str(current_location.port) if current_location.port is not None else "")
            self.ssh_path.setText(current_location.root)
            self.tabs.setCurrentIndex(1)

    def _build_ssh_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        host_row = QHBoxLayout()
        self.ssh_host = ThemedLineEdit()
        self.ssh_host.setPlaceholderText("SSH 别名或 user@host")
        self.ssh_host.setAccessibleName("SSH 主机")
        self.ssh_port = ThemedLineEdit()
        self.ssh_port.setPlaceholderText("端口")
        self.ssh_port.setAccessibleName("SSH 端口")
        self.ssh_port.setToolTip("1–65535；留空沿用 SSH 配置，未配置时使用 22")
        self.ssh_port.setFixedWidth(80)
        self.ssh_port.setInputMethodHints(Qt.InputMethodHint.ImhDigitsOnly)
        self.ssh_connect = QPushButton("连接")
        self.ssh_connect.clicked.connect(lambda: self._connect_ssh())
        self.ssh_host.returnPressed.connect(lambda: self._connect_ssh())
        self.ssh_port.returnPressed.connect(lambda: self._connect_ssh())
        host_row.addWidget(self.ssh_host, 1)
        host_row.addWidget(self.ssh_port)
        host_row.addWidget(self.ssh_connect)
        layout.addLayout(host_row)
        self.ssh_password = ThemedLineEdit()
        self.ssh_password.setPlaceholderText("登录密码（可选，仅用于本次连接）")
        self.ssh_password.setAccessibleName("SSH 登录密码")
        self.ssh_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.ssh_password.setToolTip("留空使用 SSH 配置或按提示认证；填写后优先尝试密码登录，不保存密码")
        self.ssh_password.returnPressed.connect(lambda: self._connect_ssh())
        layout.addWidget(self.ssh_password)
        self.ssh_path = ThemedLineEdit()
        self.ssh_path.setPlaceholderText("远端目录，留空使用主目录")
        self.ssh_path.setAccessibleName("远端工作区目录")
        self.ssh_path.setToolTip("例如 /home/ubuntu/project 或 C:/Users/HP/project；留空使用远端主目录")
        self.ssh_path.returnPressed.connect(lambda: self._connect_ssh())
        layout.addWidget(self.ssh_path)
        self.remote_list = QListWidget()
        self.remote_list.setAccessibleName("远端文件夹")
        self.remote_list.itemActivated.connect(self._open_directory)
        self.remote_list.itemDoubleClicked.connect(self._open_directory)
        layout.addWidget(self.remote_list, 1)
        hint = QLabel("端口留空沿用 SSH 配置，未配置时使用 22。\n支持 Windows / Linux，需 Python 3.11+；密码不保存。")
        hint.setWordWrap(True)
        hint.setProperty("muted", True)
        layout.addWidget(hint)
        self.ssh_use = QPushButton("使用此目录")
        self.ssh_use.setEnabled(False)
        self.ssh_use.clicked.connect(lambda: self._connect_ssh(select=True))
        layout.addWidget(self.ssh_use)
        self.tabs.addTab(page, "SSH 远程")
        self.ssh_connect.setEnabled(self._workspaces is not None)
        self.ssh_host.textChanged.connect(lambda: self.ssh_use.setEnabled(False))
        self.ssh_port.textChanged.connect(lambda: self.ssh_use.setEnabled(False))

    def _set_notice(self, text, *, error=False):
        self.notice.setText(text)
        self.notice.setProperty("error", error)
        self.notice.style().unpolish(self.notice)
        self.notice.style().polish(self.notice)

    def _connect_ssh(self, *, select=False):
        if self._job is not None or self._workspaces is None:
            return
        host, path = self.ssh_host.text().strip(), self.ssh_path.text().strip()
        try:
            port_text = self.ssh_port.text().strip()
            if port_text and not (port_text.isascii() and port_text.isdecimal()):
                raise ValueError("SSH 端口必须是 1–65535 的整数；留空沿用 SSH 配置")
            port = int(port_text) if port_text else None
            target = WorkspaceLocation.ssh(host, path or "/", port=port)
        except ValueError as exc:
            self._set_notice(str(exc), error=True)
            return
        self._cancel.clear()
        self.ssh_connect.setEnabled(False)
        self.ssh_use.setEnabled(False)
        self.ssh_host.setEnabled(False)
        self.ssh_port.setEnabled(False)
        self.ssh_password.setEnabled(False)
        self.ssh_path.setEnabled(False)
        # The worker consumes this once; a job cancelled before starting clears it too.
        password = [self.ssh_password.text() or None]
        self.ssh_password.clear()
        self._set_notice("正在连接并检查远端环境…")
        service = self._workspaces
        def operation():
            info = service.connect(host, port=port, password=password.pop(),
                prompt=self._request_auth, cancel_event=self._cancel)
            location = service.validate(WorkspaceLocation.ssh(host, path or info["home"], port=port).value)
            directories = [] if select else service.directories(host, WorkspaceLocation.parse(location).root, port=port)
            return location, directories
        job = BackgroundJob(operation, on_discard=lambda *_: password.clear())
        self._job = job
        def complete(result, error):
            self._job = None
            self.ssh_connect.setEnabled(True)
            self.ssh_host.setEnabled(True)
            self.ssh_port.setEnabled(True)
            self.ssh_password.setEnabled(True)
            self.ssh_path.setEnabled(True)
            if error:
                self._set_notice(str(error), error=True)
                return
            location, directories = result
            if select:
                self.selected_path = location
                self.accept()
                return
            parsed = WorkspaceLocation.parse(location)
            self.ssh_path.setText(parsed.root)
            self.remote_list.clear()
            folder = parsed.remote_path
            if folder.parent != folder:
                item = QListWidgetItem("..  上一级")
                item.setData(Qt.ItemDataRole.UserRole, folder.parent.as_posix())
                self.remote_list.addItem(item)
            for path in directories:
                item = QListWidgetItem(Icons.get_muted(Icons.FOLDER), type(folder)(path).name)
                item.setData(Qt.ItemDataRole.UserRole, path)
                self.remote_list.addItem(item)
            self.ssh_use.setEnabled(self.ssh_host.text().strip() == host)
            self._set_notice(f"已连接 {target.endpoint} · 聊天记录保存在本机")
        job.signals.finished.connect(complete)
        QThreadPool.globalInstance().start(job)

    def _open_directory(self, item):
        self.ssh_path.setText(item.data(Qt.ItemDataRole.UserRole))
        self._connect_ssh()

    def _request_auth(self, prompt, confirm):
        request = {"prompt": prompt, "confirm": confirm, "done": threading.Event(), "answer": None}
        self.auth_requested.emit(request)
        deadline = time.monotonic() + 120
        while not request["done"].wait(0.1):
            if self._cancel.is_set() or time.monotonic() >= deadline:
                request["expired"] = True
                return None
        return request["answer"]

    def _authenticate(self, request):
        if self._cancel.is_set() or request.get("expired"):
            request["done"].set()
            return
        prompt = request["prompt"]
        confirm = request["confirm"] or "yes/no" in prompt.lower()
        dialog = QDialog(self)
        dialog.setWindowTitle("确认 SSH 主机" if confirm else "SSH 身份验证")
        layout = QVBoxLayout(dialog)
        label = QLabel(prompt)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(label)
        entry = ThemedLineEdit()
        entry.setEchoMode(QLineEdit.EchoMode.Password)
        if not confirm:
            layout.addWidget(entry)
        buttons = QHBoxLayout()
        cancel = QPushButton("取消")
        cancel.clicked.connect(dialog.reject)
        accept = QPushButton("信任并连接" if confirm else "继续")
        accept.clicked.connect(dialog.accept)
        entry.returnPressed.connect(dialog.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(accept)
        layout.addLayout(buttons)
        apply_window_size(dialog, preferred=(480, 210), minimum=(350, 160))
        QTimer.singleShot(120000, dialog.reject)
        if dialog.exec() == QDialog.DialogCode.Accepted and not self._cancel.is_set() and not request.get("expired"):
            request["answer"] = "yes" if confirm else entry.text()
        entry.clear()
        request["done"].set()
        dialog.deleteLater()

    def _finished(self):
        self._cancel.set()
        self.ssh_password.clear()
        if self._job is not None:
            self._job.abandon()

    def _filter(self, text):
        visible = 0
        for index in range(self.recent_list.count()):
            item = self.recent_list.item(index)
            hidden = text.casefold() not in item.text().casefold()
            item.setHidden(hidden)
            visible += not hidden
        self._set_notice("没有匹配的最近工作区，可浏览文件夹。" if not visible else "")

    def _select_first(self):
        for index in range(self.recent_list.count()):
            item = self.recent_list.item(index)
            if not item.isHidden():
                self._select(item)
                return

    def _select(self, item):
        self._choose(item.data(Qt.ItemDataRole.UserRole))

    def _choose(self, path):
        if not path:
            return
        location = WorkspaceLocation.parse(path)
        if location.is_remote:
            self.tabs.setCurrentIndex(1)
            self.ssh_host.setText(location.host)
            self.ssh_port.setText(str(location.port) if location.port is not None else "")
            self.ssh_path.setText(location.root)
            self._connect_ssh(select=True)
            return
        if not os.path.isdir(path):
            self._set_notice("此工作区不存在或暂时无法访问，请重新选择。", error=True)
            return
        self.selected_path = path
        self.accept()

    def _browse(self):
        initial = "" if WorkspaceLocation.parse(self.current).is_remote else self.current
        self._choose(QFileDialog.getExistingDirectory(self, "选择工作区文件夹", initial))
