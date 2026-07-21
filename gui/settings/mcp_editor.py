"""Embedded MCP server catalog editor."""
from __future__ import annotations

import json
import shlex

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.app.repositories import AppRepositories
from core.app.repositories.mcp_server import McpServerRepository
from gui.settings.components import (
    SettingsActionBar,
    SettingsListDetailLayout,
    SettingsStatusListItem,
    configure_settings_resource_list,
)
from gui.utils.icon_manager import Icons
from models.contracts.mcp import McpServerConfig


class McpSettingsWidget(QWidget):
    def __init__(self, repository: McpServerRepository | None = None, parent=None) -> None:
        super().__init__(parent)
        self.repository = repository or AppRepositories.open().mcp_servers
        self.servers = [McpServerConfig.from_dict(item.to_dict()) for item in self.repository.load()]
        self._active_index = -1
        self._loading = False
        self._setup_ui()
        self.refresh_list()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        split = SettingsListDetailLayout("服务", "配置", list_stretch=2, detail_stretch=4)
        actions = SettingsActionBar(spacing=4)
        actions.add_icon_action("新增 MCP 服务", Icons.get(Icons.PLUS), self.add_server)
        self.toggle_btn = actions.add_icon_action(
            "停用 MCP 服务", Icons.get(Icons.PAUSE), self.toggle_server_enabled,
        )
        self.remove_btn = actions.add_icon_action(
            "删除 MCP 服务",
            Icons.get(Icons.XMARK, color=Icons.COLOR_ERROR),
            self.remove_server,
            danger=True,
        )
        actions.add_icon_action("重新读取 MCP 服务", Icons.get(Icons.REFRESH), self.reload_servers)
        actions.add_stretch()
        split.list_layout.addWidget(actions)
        self.list_widget = configure_settings_resource_list(QListWidget())
        self.list_widget.currentRowChanged.connect(self._on_selection_changed)
        split.list_layout.addWidget(self.list_widget, 1)

        editor = QWidget()
        form = QFormLayout(editor)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        self.name_edit = QLineEdit()
        self.command_edit = QLineEdit()
        self.args_edit = QLineEdit()
        self.args_edit.setPlaceholderText('JSON 数组或空格分隔参数，例如 ["-y", "pkg"]')
        self.env_edit = QTextEdit()
        self.env_edit.setMaximumHeight(110)
        self.env_edit.setPlaceholderText("KEY=VALUE，每行一个")
        self.cached_tools_edit = QTextEdit()
        self.cached_tools_edit.setReadOnly(True)
        self.cached_tools_edit.setPlaceholderText("运行服务后显示已发现工具")
        form.addRow("名称", self.name_edit)
        form.addRow("命令", self.command_edit)
        form.addRow("参数", self.args_edit)
        form.addRow("环境变量", self.env_edit)
        form.addRow("已发现工具", self.cached_tools_edit)
        split.add_detail_widget(editor, scrollable=True)
        root.addWidget(split, 1)

    def _clone(self, server: McpServerConfig) -> McpServerConfig:
        return McpServerConfig.from_dict(server.to_dict())

    def refresh_list(self, preferred_index: int | None = None) -> None:
        target = self._active_index if preferred_index is None else int(preferred_index)
        self._loading = True
        try:
            self.list_widget.clear()
            for server in self.servers:
                item = SettingsStatusListItem(server)
                item.set_status(
                    server.name or "未命名服务",
                    enabled=server.enabled,
                    detail=server.command or "未设置命令",
                    tooltip=self._server_tooltip(server),
                )
                self.list_widget.addItem(item)
            if self.list_widget.count():
                self.list_widget.setCurrentRow(min(max(target, 0), self.list_widget.count() - 1))
        finally:
            self._loading = False
        if self.list_widget.count():
            self._on_selection_changed(self.list_widget.currentRow())
        else:
            self._active_index = -1
            self._load_editor(None)
        self._sync_actions()

    def _on_selection_changed(self, row: int) -> None:
        if self._loading:
            return
        previous_index = self._active_index
        try:
            self._commit_active()
        except ValueError as exc:
            QMessageBox.warning(self, "MCP 配置无效", str(exc))
            self._loading = True
            try:
                self.list_widget.setCurrentRow(previous_index)
            finally:
                self._loading = False
            return
        self._active_index = int(row)
        server = self.servers[row] if 0 <= row < len(self.servers) else None
        self._load_editor(server)
        self._sync_actions()

    def _load_editor(self, server: McpServerConfig | None) -> None:
        self._loading = True
        try:
            enabled = server is not None
            for widget in (self.name_edit, self.command_edit, self.args_edit, self.env_edit, self.cached_tools_edit):
                widget.setEnabled(enabled)
            self.name_edit.setText(server.name if server else "")
            self.command_edit.setText(server.command if server else "")
            self.args_edit.setText(json.dumps(server.args, ensure_ascii=False) if server and server.args else "")
            self.env_edit.setPlainText("\n".join(f"{key}={value}" for key, value in (server.env if server else {}).items()))
            self.cached_tools_edit.setPlainText("\n".join(server.cached_tools if server else []))
        finally:
            self._loading = False

    def _commit_active(self) -> None:
        if self._loading or not (0 <= self._active_index < len(self.servers)):
            return
        current = self.servers[self._active_index]
        name = self.name_edit.text().strip()
        command = self.command_edit.text().strip()
        if not name:
            raise ValueError("服务名称不能为空")
        if not command:
            raise ValueError(f"MCP 服务“{name}”缺少启动命令")
        args_text = self.args_edit.text().strip()
        if not args_text:
            args: list[str] = []
        elif args_text.startswith("["):
            try:
                parsed = json.loads(args_text)
            except Exception as exc:
                raise ValueError(f"参数 JSON 无效：{exc}") from exc
            if not isinstance(parsed, list):
                raise ValueError("参数 JSON 必须是数组")
            args = [str(item) for item in parsed]
        else:
            try:
                args = shlex.split(args_text)
            except ValueError as exc:
                raise ValueError(f"参数格式无效：{exc}") from exc
        env: dict[str, str] = {}
        for raw in self.env_edit.toPlainText().splitlines():
            line = raw.strip()
            if not line:
                continue
            if "=" not in line:
                raise ValueError(f"环境变量必须使用 KEY=VALUE 格式：{line}")
            key, value = line.split("=", 1)
            if key.strip():
                env[key.strip()] = value.strip()
        self.servers[self._active_index] = McpServerConfig(
            name=name,
            command=command,
            args=args,
            env=env,
            enabled=current.enabled,
            cached_tools=list(current.cached_tools or []),
        )

    def add_server(self) -> None:
        try:
            self._commit_active()
        except ValueError as exc:
            QMessageBox.warning(self, "MCP 配置无效", str(exc))
            return
        existing = {server.name for server in self.servers}
        index = 1
        name = "new-mcp"
        while name in existing:
            index += 1
            name = f"new-mcp-{index}"
        self.servers.append(McpServerConfig(name=name, command="npx", enabled=True))
        self._active_index = len(self.servers) - 1
        self.refresh_list(self._active_index)
        self.name_edit.selectAll()
        self.name_edit.setFocus()

    def toggle_server_enabled(self) -> None:
        if not (0 <= self._active_index < len(self.servers)):
            return
        try:
            self._commit_active()
        except ValueError as exc:
            QMessageBox.warning(self, "MCP 配置无效", str(exc))
            return
        server = self.servers[self._active_index]
        server.enabled = not server.enabled
        self.refresh_list(self._active_index)

    def remove_server(self) -> None:
        if not (0 <= self._active_index < len(self.servers)):
            return
        server = self.servers[self._active_index]
        if QMessageBox.question(self, "删除 MCP 服务", f'确定删除“{server.name}”吗？') != QMessageBox.StandardButton.Yes:
            return
        del self.servers[self._active_index]
        self._active_index = min(self._active_index, len(self.servers) - 1)
        self.refresh_list(self._active_index)

    def reload_servers(self) -> None:
        self.servers = [self._clone(server) for server in self.repository.load()]
        self._active_index = 0 if self.servers else -1
        self.refresh_list(self._active_index)

    def collect_servers(self) -> list[McpServerConfig]:
        self._commit_active()
        names: set[str] = set()
        for server in self.servers:
            if server.name in names:
                raise ValueError(f"MCP 服务名称重复：{server.name}")
            names.add(server.name)
        return [self._clone(server) for server in self.servers]

    def _sync_actions(self) -> None:
        server = self.servers[self._active_index] if 0 <= self._active_index < len(self.servers) else None
        enabled = server is not None
        self.toggle_btn.setEnabled(enabled)
        self.remove_btn.setEnabled(enabled)
        action_label = "停用 MCP 服务" if server and server.enabled else "启用 MCP 服务"
        self.toggle_btn.setToolTip(action_label)
        self.toggle_btn.setAccessibleName(action_label)
        self.toggle_btn.setIcon(Icons.get(Icons.PAUSE if server and server.enabled else Icons.PLAY))

    @staticmethod
    def _server_tooltip(server: McpServerConfig) -> str:
        return (
            f"{server.name}\n"
            f"状态：{'启用' if server.enabled else '停用'}\n"
            f"命令：{server.command or '-'}\n"
            f"参数：{' '.join(server.args or []) or '-'}"
        )
