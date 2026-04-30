"""MCP 服务管理对话框与设置面板。"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QListWidget, QListWidgetItem,
    QCheckBox, QMessageBox, QWidget, QFormLayout, QTextEdit, QGroupBox
)
from PyQt6.QtCore import Qt
from typing import List, Optional
import json

from models.mcp_server import McpServerConfig
from services.storage_service import StorageService
from ui.utils.icon_manager import Icons

class McpServerEditDialog(QDialog):
    def __init__(self, config: Optional[McpServerConfig] = None, parent=None):
        super().__init__(parent)
        self.config = config
        self.setup_ui()
        if config:
            self.load_config(config)

    def setup_ui(self):
        self.setObjectName("mcpServerDialog")
        self.setWindowTitle("编辑 MCP 服务" if self.config else "新增 MCP 服务")
        self.setMinimumSize(520, 420)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        intro = QLabel("配置通过 stdio 启动的 MCP 服务。命令、参数与环境变量会在运行时注入到 MCP 客户端。")
        intro.setWordWrap(True)
        intro.setProperty("muted", True)
        root.addWidget(intro)

        form_group = QGroupBox("服务配置")
        layout = QFormLayout(form_group)
        layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(8)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：filesystem / playwright / docs")
        self.command_edit = QLineEdit()
        self.command_edit.setPlaceholderText("例如：python、node、uvx、npx")
        self.args_edit = QLineEdit()
        self.args_edit.setPlaceholderText('空格分隔，或填写 JSON 数组，例如 ["-m", "server"]')
        self.env_edit = QTextEdit()
        self.env_edit.setPlaceholderText('KEY=VALUE，每行一个，例如\nAPI_BASE=https://example.com\nTOKEN=xxx')
        self.env_edit.setMaximumHeight(110)
        self.enabled_check = QCheckBox("启用此服务")
        self.enabled_check.setChecked(True)

        layout.addRow("名称", self.name_edit)
        layout.addRow("命令", self.command_edit)
        layout.addRow("参数", self.args_edit)
        layout.addRow("环境变量", self.env_edit)
        layout.addRow("", self.enabled_check)

        root.addWidget(form_group)

        hint = QLabel("参数既支持简单空格分隔，也支持 JSON 数组；环境变量仅解析包含 `=` 的行。")
        hint.setWordWrap(True)
        hint.setProperty("muted", True)
        root.addWidget(hint)

        btn_layout = QHBoxLayout()
        save_btn = QPushButton("Save")
        save_btn.setText("保存")
        save_btn.setProperty("primary", True)
        save_btn.setIcon(Icons.get(Icons.CHECK, scale_factor=1.0))
        save_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("取消")
        cancel_btn.setIcon(Icons.get(Icons.XMARK, scale_factor=1.0))
        cancel_btn.clicked.connect(self.reject)

        btn_layout.addStretch()
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(save_btn)
        root.addLayout(btn_layout)

    def load_config(self, config: McpServerConfig):
        self.name_edit.setText(config.name)
        self.command_edit.setText(config.command)
        if config.args:
            self.args_edit.setText(json.dumps(config.args))
        
        env_str = []
        for k, v in config.env.items():
            env_str.append(f"{k}={v}")
        self.env_edit.setPlainText("\n".join(env_str))
        self.enabled_check.setChecked(config.enabled)

    def get_config(self) -> McpServerConfig:
        name = self.name_edit.text().strip() or "Unnamed"
        command = self.command_edit.text().strip()
        
        args_text = self.args_edit.text().strip()
        args = []
        if args_text:
            if args_text.startswith("["):
                try:
                    args = json.loads(args_text)
                except:
                    args = args_text.split()
            else:
                args = args_text.split()
        
        env = {}
        env_lines = self.env_edit.toPlainText().split('\n')
        for line in env_lines:
            if '=' in line:
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
                
        return McpServerConfig(
            name=name,
            command=command,
            args=args,
            env=env,
            enabled=self.enabled_check.isChecked()
        )

    def accept(self) -> None:
        if not (self.command_edit.text() or "").strip():
            QMessageBox.warning(self, "命令不能为空", "请填写 MCP 服务的启动命令。")
            return
        super().accept()


class McpSettingsWidget(QWidget):
    def __init__(self, storage_service: Optional[StorageService] = None, parent=None):
        super().__init__(parent)
        self.storage = storage_service or StorageService()
        self.servers = self.storage.load_mcp_servers()
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.count_label = QLabel("")
        self.count_label.setProperty("muted", True)
        header.addWidget(self.count_label)
        header.addStretch()

        add_btn = QPushButton("新增")
        add_btn.setIcon(Icons.get(Icons.PLUS, scale_factor=1.0))
        add_btn.clicked.connect(self.add_server)
        header.addWidget(add_btn)

        edit_btn = QPushButton("编辑")
        edit_btn.setIcon(Icons.get(Icons.EDIT, scale_factor=1.0))
        edit_btn.clicked.connect(self.edit_server)
        header.addWidget(edit_btn)

        remove_btn = QPushButton("删除")
        remove_btn.setIcon(Icons.get(Icons.TRASH, color=Icons.COLOR_ERROR, scale_factor=1.0))
        remove_btn.setProperty("danger", True)
        remove_btn.clicked.connect(self.remove_server)
        header.addWidget(remove_btn)

        reload_btn = QPushButton("重载")
        reload_btn.setIcon(Icons.get(Icons.REFRESH, scale_factor=1.0))
        reload_btn.clicked.connect(self.reload_servers)
        header.addWidget(reload_btn)

        layout.addLayout(header)

        content = QHBoxLayout()
        content.setSpacing(12)

        list_group = QGroupBox("服务列表")
        list_layout = QVBoxLayout(list_group)
        list_layout.setContentsMargins(10, 10, 10, 10)
        list_layout.setSpacing(8)

        self.list_widget = QListWidget()
        self.list_widget.setObjectName("settings_list")
        self.list_widget.setSpacing(2)
        self.list_widget.itemDoubleClicked.connect(self.edit_server)
        self.list_widget.currentItemChanged.connect(self._on_selection_changed)
        list_layout.addWidget(self.list_widget)
        content.addWidget(list_group, 2)

        detail_group = QGroupBox("服务详情")
        detail_layout = QVBoxLayout(detail_group)
        detail_layout.setContentsMargins(10, 10, 10, 10)
        detail_layout.setSpacing(8)

        self.detail_title = QLabel("选择左侧服务查看详情")
        self.detail_title.setProperty("heading", True)
        detail_layout.addWidget(self.detail_title)

        self.detail_meta = QLabel("")
        self.detail_meta.setWordWrap(True)
        self.detail_meta.setProperty("muted", True)
        detail_layout.addWidget(self.detail_meta)

        self.detail_args = QLabel("")
        self.detail_args.setWordWrap(True)
        detail_layout.addWidget(self.detail_args)

        self.detail_env = QTextEdit()
        self.detail_env.setReadOnly(True)
        self.detail_env.setPlaceholderText("当前服务没有环境变量")
        self.detail_env.setMaximumHeight(160)
        detail_layout.addWidget(self.detail_env)

        detail_hint = QLabel("双击列表可直接编辑。建议为每个服务使用稳定名称，便于在问题排查和日志中识别。")
        detail_hint.setWordWrap(True)
        detail_hint.setProperty("muted", True)
        detail_layout.addWidget(detail_hint)
        detail_layout.addStretch()
        content.addWidget(detail_group, 3)

        layout.addLayout(content)

        self.refresh_list()

    def refresh_list(self):
        current_name = ""
        current_item = self.list_widget.currentItem()
        if current_item is not None:
            current_server = current_item.data(Qt.ItemDataRole.UserRole)
            current_name = str(getattr(current_server, "name", "") or "")

        self.list_widget.clear()
        for s in self.servers:
            status_icon = Icons.get_success(Icons.CIRCLE_CHECK) if s.enabled else Icons.get_error(Icons.CIRCLE_XMARK)
            status_text = "已启用" if s.enabled else "已停用"
            item = QListWidgetItem(f"{s.name} · {status_text} · {s.command}")
            item.setIcon(status_icon)
            item.setToolTip(self._server_tooltip(s))
            item.setData(Qt.ItemDataRole.UserRole, s)
            self.list_widget.addItem(item)

        self.count_label.setText(f"共 {len(self.servers)} 个 MCP 服务")

        if self.list_widget.count() == 0:
            self._update_detail_panel(None)
            return

        restore_row = 0
        if current_name:
            for index in range(self.list_widget.count()):
                server = self.list_widget.item(index).data(Qt.ItemDataRole.UserRole)
                if str(getattr(server, "name", "") or "") == current_name:
                    restore_row = index
                    break
        self.list_widget.setCurrentRow(restore_row)

    def add_server(self):
        dialog = McpServerEditDialog(parent=self)
        if dialog.exec():
            new_server = dialog.get_config()
            self.servers.append(new_server)
            self.save_and_refresh()

    def edit_server(self, item=None):
        if item is None:
            item = self.list_widget.currentItem()
        if item is None:
            return
        server = item.data(Qt.ItemDataRole.UserRole)
        dialog = McpServerEditDialog(config=server, parent=self)
        if dialog.exec():
            new_config = dialog.get_config()
            idx = self.servers.index(server)
            self.servers[idx] = new_config
            self.save_and_refresh()

    def remove_server(self):
        item = self.list_widget.currentItem()
        if item is None:
            return
        server = item.data(Qt.ItemDataRole.UserRole)
        if QMessageBox.question(
            self,
            "删除 MCP 服务",
            f'确定删除 "{server.name}" 吗？',
        ) != QMessageBox.StandardButton.Yes:
            return
        self.servers = [s for s in self.servers if s is not server]
        self.save_and_refresh()

    def reload_servers(self):
        self.servers = self.storage.load_mcp_servers()
        self.refresh_list()

    def save_and_refresh(self):
        self.storage.save_mcp_servers(self.servers)
        self.refresh_list()

    def _on_selection_changed(self, current, _previous) -> None:
        server = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        self._update_detail_panel(server)

    def _update_detail_panel(self, server: Optional[McpServerConfig]) -> None:
        if server is None:
            self.detail_title.setText("选择左侧服务查看详情")
            self.detail_meta.setText("")
            self.detail_args.setText("")
            self.detail_env.clear()
            return

        self.detail_title.setText(server.name or "未命名服务")
        status_text = "已启用" if server.enabled else "已停用"
        self.detail_meta.setText(
            f"状态：{status_text}\n命令：{server.command or '-'}\n参数数：{len(server.args or [])} · 环境变量数：{len(server.env or {})}"
        )
        args_text = " ".join(server.args or []) if server.args else "（无附加参数）"
        self.detail_args.setText(f"参数：{args_text}")
        env_text = "\n".join(f"{k}={v}" for k, v in (server.env or {}).items())
        self.detail_env.setPlainText(env_text)

    @staticmethod
    def _server_tooltip(server: McpServerConfig) -> str:
        status_text = "已启用" if server.enabled else "已停用"
        args_text = " ".join(server.args or []) or "-"
        env_text = ", ".join((server.env or {}).keys()) or "-"
        return (
            f"名称: {server.name}\n"
            f"状态: {status_text}\n"
            f"命令: {server.command or '-'}\n"
            f"参数: {args_text}\n"
            f"环境变量: {env_text}"
        )
