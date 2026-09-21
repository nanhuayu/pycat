"""Embedded MCP server catalog editor."""
from __future__ import annotations

import json
import shlex
from collections.abc import Callable, Iterable

from PyQt6 import sip
from PyQt6.QtCore import QThreadPool
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QMenu,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QSizePolicy,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pycat.gui.resources.tool_catalog import ToolCatalog
from pycat.gui.runtime.background_job import BackgroundJob
from pycat.gui.settings.components import SettingsListDetailLayout, RESOURCE_DESCRIPTION_ROLE, RESOURCE_TOGGLE_ROLE
from pycat.gui.settings.components import (
    SettingsActionBar,
    SettingsStatusListItem,
    configure_settings_resource_list,
)
from pycat.gui.utils.settings_controls import SettingsFormLayout
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.widgets.themed_line_edit import ThemedLineEdit, ThemedTextEdit
from pycat.models.contracts.mcp import (
    TRANSPORT_SSE,
    TRANSPORT_STDIO,
    TRANSPORT_STREAMABLE_HTTP,
    McpServerConfig,
)

_TRANSPORT_LABELS = {
    TRANSPORT_STDIO: "本地进程 (stdio)",
    TRANSPORT_STREAMABLE_HTTP: "流式 HTTP (streamable_http)",
    TRANSPORT_SSE: "SSE (遗留)",
}

def _parse_arguments(text: str) -> list[str]:
    value = text.strip()
    if not value:
        return []
    if value.startswith('['):
        try:
            result = json.loads(value)
        except ValueError as exc:
            raise ValueError(f'参数 JSON 无效：{exc}') from exc
        if not isinstance(result, list) or any(not isinstance(item, str) for item in result):
            raise ValueError('参数 JSON 必须是字符串数组')
        return result
    try:
        return shlex.split(value)
    except ValueError as exc:
        raise ValueError(f'参数格式无效：{exc}') from exc


def _parse_json_object(text: str, field_label: str) -> dict[str, str]:
    """Parse a JSON object mapping string keys to string values.

    Empty input means ``{}``.  The format matches the ecosystem ``mcp.json``
    payload and the contract serialization, so values can be pasted and
    copied between all three without conversion.
    """
    stripped = str(text or "").strip()
    if not stripped:
        return {}
    try:
        parsed = json.loads(stripped)
    except Exception as exc:
        raise ValueError(f"{field_label}必须是合法的 JSON 对象，例如 {{\"KEY\": \"VALUE\"}}：{exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_label}必须是 JSON 对象，例如 {{\"KEY\": \"VALUE\"}}")
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError(f"{field_label}的键和值都必须是字符串")
    return dict(parsed)


def _format_json_object(mapping: dict[str, str]) -> str:
    if not mapping:
        return "{}"
    return json.dumps(dict(mapping or {}), ensure_ascii=False, indent=2)


class McpCreateDialog(QDialog):
    """Collect the minimal fields for one valid MCP server draft entry."""

    def __init__(self, existing_names: Iterable[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("新增 MCP 服务")
        self.setModal(True)
        self.setMinimumWidth(460)
        self._existing = {str(name or "").strip() for name in existing_names}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        form = SettingsFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

        base = 1
        base_name = 'new-mcp'
        name = base_name
        while name in self._existing:
            base += 1
            name = f'{base_name}-{base}'

        self.name_edit = ThemedLineEdit()
        self.name_edit.setText(name)
        self.name_edit.setPlaceholderText("服务名称，例如 filesystem")

        self.transport_combo = QComboBox()
        for key, label in _TRANSPORT_LABELS.items():
            self.transport_combo.addItem(label, key)
        self.transport_combo.currentIndexChanged.connect(self._sync_fields)

        self.command_edit = ThemedLineEdit()
        self.command_edit.setPlaceholderText("启动命令，例如 npx 或 python")
        self.args_edit = ThemedLineEdit()
        self.args_edit.setPlaceholderText('参数，例如 ["-y", "pkg"]')
        self.url_edit = ThemedLineEdit()
        self.url_edit.setPlaceholderText("https://example.com/mcp")

        form.addRow("名称", self.name_edit)
        form.addRow("传输方式", self.transport_combo)
        self._command_row = QLabel("命令")
        form.addRow(self._command_row, self.command_edit)
        self._args_row = QLabel('参数')
        form.addRow(self._args_row, self.args_edit)
        self._url_row = QLabel("服务 URL")
        form.addRow(self._url_row, self.url_edit)
        layout.addLayout(form)
        self.validation_label = QLabel("")
        self.validation_label.setObjectName("validation_error_label")
        self.validation_label.setWordWrap(True)
        self.validation_label.setVisible(False)
        layout.addWidget(self.validation_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("创建")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._sync_fields()

    def _sync_fields(self) -> None:
        is_stdio = self.transport() == TRANSPORT_STDIO
        self._command_row.setVisible(is_stdio)
        self.command_edit.setVisible(is_stdio)
        self._args_row.setVisible(is_stdio)
        self.args_edit.setVisible(is_stdio)
        self._url_row.setVisible(not is_stdio)
        self.url_edit.setVisible(not is_stdio)

    def transport(self) -> str:
        return str(self.transport_combo.currentData() or TRANSPORT_STDIO)

    def accept(self) -> None:
        """Validate the draft before closing the modal dialog."""

        try:
            self.values()
        except ValueError as exc:
            message = str(exc)
            self.validation_label.setText(message)
            self.validation_label.setVisible(bool(message))
            transport = self.transport()
            if not self.name_edit.text().strip():
                self.name_edit.setFocus()
            elif transport == TRANSPORT_STDIO and not self.command_edit.text().strip():
                self.command_edit.setFocus()
            elif transport != TRANSPORT_STDIO and not self.url_edit.text().strip():
                self.url_edit.setFocus()
            return
        self.validation_label.clear()
        self.validation_label.setVisible(False)
        super().accept()

    def values(self) -> McpServerConfig:
        name = self.name_edit.text().strip()
        transport = self.transport()
        config = McpServerConfig(
            name=name,
            transport=transport,
            command=self.command_edit.text().strip() if transport == TRANSPORT_STDIO else "",
            args=_parse_arguments(self.args_edit.text()) if transport == TRANSPORT_STDIO else [],
            url=self.url_edit.text().strip() if transport != TRANSPORT_STDIO else "",
            enabled=False,
        )
        config.validate()
        if name in self._existing:
            raise ValueError(f"MCP 服务名称重复：{name}")
        return config


class McpSettingsWidget(QWidget):
    def __init__(
        self,
        servers: Iterable[McpServerConfig] = (),
        *,
        reload_provider: Callable[[], Iterable[McpServerConfig]] | None = None,
        connection_tester: Callable[[McpServerConfig], dict] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._reload_provider = reload_provider
        self._connection_tester = connection_tester
        self.servers = [self._clone(item) for item in servers]
        self._active_index = -1
        self._probe_job = None
        self._loading = False
        self._setup_ui()
        self.refresh_list()
        self._loaded_fingerprint = self._servers_fingerprint()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        split = SettingsListDetailLayout()
        self.browser = split
        overview = QHBoxLayout()
        self.search = ThemedLineEdit()
        self.search.setPlaceholderText("搜索已安装 MCP")
        self.search.setAccessibleName("搜索已安装 MCP")
        self.search.textChanged.connect(self._filter_list)
        overview.addWidget(self.search, 1)
        add = QPushButton("＋ 添加")
        menu = QMenu(add)
        menu.addAction("手动配置", self.add_server)
        menu.addAction("导入 mcp.json", self.import_servers)
        add.setMenu(menu)
        overview.addWidget(add)
        more = QPushButton("更多")
        menu = QMenu(more)
        menu.addAction("导出 mcp.json", self.export_servers)
        menu.addAction("重新读取", self.reload_servers)
        more.setMenu(menu)
        overview.addWidget(more)
        split.toolbar_layout.addLayout(overview)
        actions = SettingsActionBar(spacing=4)
        self.toggle_btn = actions.add_action(
            "停用", Icons.get(Icons.PAUSE), self.toggle_server_enabled,
        )
        self.remove_btn = actions.add_icon_action(
            "删除 MCP 服务",
            Icons.get(Icons.XMARK, color=Icons.COLOR_ERROR),
            self.remove_server,
            danger=True,
        )
        actions.add_stretch()
        self.test_btn = QPushButton("测试连接")
        self.test_btn.setToolTip("用当前草稿测试连接和读取工具目录")
        self.test_btn.clicked.connect(self._test_connection)
        actions.add_widget(self.test_btn)
        split.detail_layout.addWidget(actions)
        self.list_widget = configure_settings_resource_list(QListWidget())
        self.list_widget.currentRowChanged.connect(self._on_selection_changed)
        split.list_layout.addWidget(self.list_widget, 1)
        split.bind(self.list_widget, self.toggle_server_enabled)

        editor = QWidget()
        form = QGridLayout(editor)
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(16)
        form.setColumnStretch(0, 1)
        form.setColumnStretch(1, 1)
        def field(label, widget, row, column=0, span=1):
            container = QWidget()
            column_layout = QVBoxLayout(container)
            column_layout.setContentsMargins(0, 0, 0, 0)
            column_layout.setSpacing(6)
            column_layout.addWidget(label if isinstance(label, QLabel) else QLabel(label))
            column_layout.addWidget(widget)
            widget.setMinimumWidth(0)
            widget.setSizePolicy(QSizePolicy.Policy.Ignored, widget.sizePolicy().verticalPolicy())
            form.addWidget(container, row, column, 1, span)
            return container
        self.name_edit = ThemedLineEdit()
        self.transport_combo = QComboBox()
        for key, label in _TRANSPORT_LABELS.items():
            self.transport_combo.addItem(label, key)
        self.transport_combo.currentIndexChanged.connect(self._on_transport_changed)
        self.command_edit = ThemedLineEdit()
        self.args_edit = ThemedLineEdit()
        self.args_edit.setPlaceholderText('JSON 数组或空格分隔参数，例如 ["-y", "pkg"]')
        self.cwd_edit = ThemedLineEdit()
        self.cwd_edit.setPlaceholderText("工作目录（可选，留空继承当前目录）")
        self.env_edit = ThemedTextEdit()
        self.env_edit.setMaximumHeight(110)
        self.env_edit.setPlaceholderText('JSON 对象，默认 {}，例如 {"API_KEY": "value"}')
        self.url_edit = ThemedLineEdit()
        self.url_edit.setPlaceholderText("https://example.com/mcp")
        self.headers_edit = ThemedTextEdit()
        self.headers_edit.setMaximumHeight(110)
        self.headers_edit.setPlaceholderText(
            'JSON 对象，默认 {}，例如 {"Authorization": "Bearer xxx"}'
        )
        self._command_label = QLabel("命令")
        self._args_label = QLabel("参数")
        self._cwd_label = QLabel("工作目录")
        self._env_label = QLabel("环境变量")
        self._url_label = QLabel("服务 URL")
        self._headers_label = QLabel("请求头")
        field("名称", self.name_edit, 0)
        field("传输方式", self.transport_combo, 0, 1)
        self._stdio_fields = [field(self._command_label, self.command_edit, 1),
            field(self._args_label, self.args_edit, 1, 1),
            field(self._cwd_label, self.cwd_edit, 2, span=2),
            field(self._env_label, self.env_edit, 3, span=2)]
        self._remote_fields = [field(self._url_label, self.url_edit, 4, span=2),
            field(self._headers_label, self.headers_edit, 5, span=2)]
        self.tool_catalog = ToolCatalog()
        form.addWidget(self.tool_catalog, 6, 0, 1, 2)
        form.setRowStretch(7, 1)
        split.add_detail_widget(editor, scrollable=True)
        root.addWidget(split, 1)

    def _clone(self, server: McpServerConfig) -> McpServerConfig:
        return McpServerConfig.from_dict(server.to_dict())

    def refresh_list(self, preferred_index: int | None = None) -> None:
        self.cancel_probe()
        target = self._active_index if preferred_index is None else int(preferred_index)
        selected_row = -1
        self._loading = True
        try:
            self.list_widget.clear()
            for server in self.servers:
                item = SettingsStatusListItem(server)
                transport = server.normalized_transport()
                item.set_status(
                    server.name or "未命名服务",
                    enabled=server.enabled,
                    detail=f"{transport} · {'已启用' if server.enabled else '已停用'}",
                    tooltip=self._server_tooltip(server),
                )
                item.setData(RESOURCE_DESCRIPTION_ROLE, server.endpoint_summary())
                item.setData(RESOURCE_TOGGLE_ROLE, "mcp")
                self.list_widget.addItem(item)
            if self.list_widget.count():
                selected_row = min(max(target, 0), self.list_widget.count() - 1)
                self.list_widget.setCurrentRow(selected_row)
        finally:
            self._loading = False
        if selected_row >= 0:
            self._active_index = selected_row
            self._load_editor(self.servers[selected_row])
        else:
            self._active_index = -1
            self._load_editor(None)
        self._sync_actions()
        self._filter_list()

    def _filter_list(self):
        query = self.search.text().casefold()
        for index in range(self.list_widget.count()):
            item = self.list_widget.item(index)
            item.setHidden(query not in (item.text() + " " + str(item.data(RESOURCE_DESCRIPTION_ROLE) or "")).casefold())

    def _on_selection_changed(self, row: int) -> None:
        if self._loading:
            return
        self.cancel_probe()
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
            widgets = (
                self.name_edit, self.transport_combo, self.command_edit, self.args_edit,
                self.cwd_edit, self.env_edit, self.url_edit, self.headers_edit,
                self.tool_catalog, self.test_btn,
            )
            for widget in widgets:
                widget.setEnabled(enabled)
            self.name_edit.setText(server.name if server else "")
            index = self.transport_combo.findData(
                server.normalized_transport() if server else TRANSPORT_STDIO
            )
            self.transport_combo.setCurrentIndex(max(index, 0))
            self.command_edit.setText(server.command if server else "")
            self.args_edit.setText(json.dumps(server.args, ensure_ascii=False) if server and server.args else "")
            self.cwd_edit.setText(server.cwd if server else "")
            self.env_edit.setPlainText(_format_json_object(server.env if server else {}))
            self.url_edit.setText(server.url if server else "")
            self.headers_edit.setPlainText(_format_json_object(server.headers if server else {}))
            self.tool_catalog.set_tools(server.cached_tools if server else [])
        finally:
            self._loading = False
        self._sync_transport_fields()

    def _on_transport_changed(self) -> None:
        if self._loading:
            return
        self._sync_transport_fields()

    def _sync_transport_fields(self) -> None:
        is_stdio = str(self.transport_combo.currentData() or TRANSPORT_STDIO) == TRANSPORT_STDIO
        for widget in self._stdio_fields:
            widget.setVisible(is_stdio)
        for widget in self._remote_fields:
            widget.setVisible(not is_stdio)

    def _commit_active(self) -> None:
        if self._loading or not (0 <= self._active_index < len(self.servers)):
            return
        current = self.servers[self._active_index]
        name = self.name_edit.text().strip()
        transport = str(self.transport_combo.currentData() or TRANSPORT_STDIO)
        command = self.command_edit.text().strip()
        url = self.url_edit.text().strip()
        args = _parse_arguments(self.args_edit.text())
        env = _parse_json_object(self.env_edit.toPlainText(), "环境变量")
        headers = _parse_json_object(self.headers_edit.toPlainText(), "请求头")
        candidate = McpServerConfig(
            name=name,
            transport=transport,
            command=command if transport == TRANSPORT_STDIO else "",
            args=args if transport == TRANSPORT_STDIO else [],
            env=env if transport == TRANSPORT_STDIO else {},
            cwd=self.cwd_edit.text().strip() if transport == TRANSPORT_STDIO else "",
            url=url if transport != TRANSPORT_STDIO else "",
            headers=headers if transport != TRANSPORT_STDIO else {},
            enabled=current.enabled,
            cached_tools=list(current.cached_tools or []),
            integration=current.integration if transport == TRANSPORT_STDIO else "",
        )
        candidate.validate()
        self.servers[self._active_index] = candidate

    def _test_connection(self) -> None:
        """Probe the current form config with one real discovery round."""

        if self._connection_tester is None:
            QMessageBox.information(
                self, "测试连接", "当前环境未提供 MCP 运行时，无法测试连接。"
            )
            return
        if not (0 <= self._active_index < len(self.servers)):
            return
        try:
            self._commit_active()
        except ValueError as exc:
            QMessageBox.warning(self, "MCP 配置无效", str(exc))
            return
        if self._probe_job is not None:
            return
        config = self._clone(self.servers[self._active_index])
        self._probe_config = config
        self.test_btn.setEnabled(False)
        self.test_btn.setText("测试中…")
        tester = self._connection_tester
        job = BackgroundJob(lambda: tester(config))
        self._probe_job = job
        self.destroyed.connect(job.abandon)
        job.signals.finished.connect(lambda result, error: self._finish_probe(job, result, error))
        QThreadPool.globalInstance().start(job)

    def _finish_probe(self, job, result, error):
        if sip.isdeleted(self) or self._probe_job is not job:
            return
        self._probe_job = None
        self.destroyed.disconnect(job.abandon)
        self.test_btn.setEnabled(True)
        self.test_btn.setText("测试连接")
        try:
            self._commit_active()
        except ValueError:
            return
        if not 0 <= self._active_index < len(self.servers):
            return
        config = self.servers[self._active_index]
        if config.to_dict() != self._probe_config.to_dict():
            return
        result = result or {"ok": False, "error": str(error or "连接测试未返回结果")}
        if result.get("ok"):
            tools = [str(name) for name in result.get("tools") or []]
            config.cached_tools = tools
            self.tool_catalog.set_tools(tools, result.get("schemas") or ())
            QMessageBox.information(
                self,
                "测试连接",
                f"连接成功，发现 {len(tools)} 个工具。",
            )
        else:
            error = str(result.get("error") or "未知错误")
            while error.startswith(('连接失败：', '连接失败:')):
                error = error[5:].lstrip()
            QMessageBox.warning(
                self,
                "MCP 连接失败",
                error or '未知错误',
            )

    def cancel_probe(self):
        job, self._probe_job = self._probe_job, None
        if job is not None:
            job.abandon()
            self.destroyed.disconnect(job.abandon)
            self.test_btn.setEnabled(True)
            self.test_btn.setText("测试连接")

    def hideEvent(self, event):
        self.cancel_probe()
        super().hideEvent(event)

    def add_server(self) -> None:
        try:
            self._commit_active()
        except ValueError as exc:
            QMessageBox.warning(self, "MCP 配置无效", str(exc))
            return
        dialog = McpCreateDialog((server.name for server in self.servers), parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            config = dialog.values()
        except ValueError as exc:
            QMessageBox.warning(self, "MCP 配置无效", str(exc))
            return
        self.servers.append(config)
        self._active_index = len(self.servers) - 1
        self.refresh_list(self._active_index)
        self.browser.show_detail()
        self.name_edit.selectAll()
        self.name_edit.setFocus()

    def import_servers(self) -> None:
        path, _selected = QFileDialog.getOpenFileName(
            self, "导入 mcp.json", "", "MCP 配置 (*.json);;所有文件 (*)"
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception as exc:
            QMessageBox.warning(self, "导入失败", f"无法读取文件：{exc}")
            return
        entries = payload.get("mcpServers") if isinstance(payload, dict) else None
        if not isinstance(entries, dict):
            QMessageBox.warning(self, "导入失败", "文件必须是 {\"mcpServers\": {...}} 格式。")
            return
        try:
            self._commit_active()
        except ValueError as exc:
            QMessageBox.warning(self, "MCP 配置无效", str(exc))
            return
        imported: list[McpServerConfig] = []
        errors: list[str] = []
        existing = {server.name for server in self.servers}
        for name, raw in entries.items():
            if not isinstance(name, str):
                errors.append("服务名称必须是字符串")
                continue
            if not isinstance(raw, dict):
                errors.append(f"{name}：条目不是对象")
                continue
            try:
                candidate = McpServerConfig.from_mcp_json(name, raw)
            except ValueError as exc:
                errors.append(f"{name}：{exc}")
                continue
            if candidate.name in existing:
                errors.append(f"MCP 服务名称重复：{candidate.name}")
                continue
            existing.add(candidate.name)
            imported.append(candidate)
        if imported:
            self.servers.extend(imported)
            self._active_index = len(self.servers) - 1
            self.refresh_list(self._active_index)
        summary = f"导入 {len(imported)} 个服务。"
        if errors:
            summary += "\n以下条目被跳过：\n" + "\n".join(errors)
        QMessageBox.information(self, "导入 mcp.json", summary)

    def export_servers(self) -> None:
        try:
            servers = self.collect_servers()
        except ValueError as exc:
            QMessageBox.warning(self, "MCP 配置无效", str(exc))
            return
        enabled_servers = [server for server in servers if server.enabled]
        if not enabled_servers:
            QMessageBox.information(self, "导出 mcp.json", "没有已启用的 MCP 服务可导出。")
            return
        payload = {"mcpServers": {server.name: self._to_mcp_json(server) for server in enabled_servers}}
        path, _selected = QFileDialog.getSaveFileName(
            self, "导出 mcp.json", "mcp.json", "MCP 配置 (*.json);;所有文件 (*)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
        except Exception as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        QMessageBox.information(
            self,
            "导出 mcp.json",
            f"已导出 {len(enabled_servers)} 个服务到：\n{path}\n\n注意：导出内容包含请求头中的凭据，请自行妥善保管。",
        )

    @staticmethod
    def _to_mcp_json(server: McpServerConfig) -> dict:
        return server.to_mcp_json()

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
        if self._reload_provider is None:
            return
        if self.has_unsaved_changes():
            answer = QMessageBox.question(
                self,
                "放弃 MCP 更改",
                "重新读取会放弃尚未保存的 MCP 更改，是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.servers = [self._clone(server) for server in self._reload_provider()]
        self._active_index = 0 if self.servers else -1
        self.refresh_list(self._active_index)
        self._loaded_fingerprint = self._servers_fingerprint()

    def collect_servers(self) -> list[McpServerConfig]:
        self._commit_active()
        names: set[str] = set()
        for server in self.servers:
            server.validate()
            if server.name in names:
                raise ValueError(f"MCP 服务名称重复：{server.name}")
            names.add(server.name)
        return [self._clone(server) for server in self.servers]

    def has_unsaved_changes(self) -> bool:
        try:
            self._commit_active()
        except ValueError:
            return True
        return self._servers_fingerprint() != self._loaded_fingerprint

    def mark_saved(self) -> None:
        self._commit_active()
        self._loaded_fingerprint = self._servers_fingerprint()

    def _servers_fingerprint(self) -> str:
        return json.dumps(
            [server.to_dict() for server in self.servers],
            ensure_ascii=False,
            sort_keys=True,
        )

    def _sync_actions(self) -> None:
        server = self.servers[self._active_index] if 0 <= self._active_index < len(self.servers) else None
        enabled = server is not None
        self.toggle_btn.setEnabled(enabled)
        self.remove_btn.setEnabled(enabled)
        action_label = "停用 MCP 服务" if server and server.enabled else "启用 MCP 服务"
        self.toggle_btn.setText("停用" if server and server.enabled else "启用")
        self.toggle_btn.setToolTip(action_label)
        self.toggle_btn.setAccessibleName(action_label)
        self.toggle_btn.setIcon(Icons.get(Icons.PAUSE if server and server.enabled else Icons.PLAY))

    @staticmethod
    def _server_tooltip(server: McpServerConfig) -> str:
        transport = server.normalized_transport()
        lines = [
            f"{server.name}",
            f"状态：{'启用' if server.enabled else '停用'}",
            f"传输：{transport}",
        ]
        if server.is_http_transport():
            lines.append(f"URL：{server.url or '-'}")
            if server.headers:
                lines.append(f"请求头：{', '.join(sorted(server.headers))}")
        else:
            lines.append(f"命令：{server.command or '-'}")
            lines.append(f"参数：{' '.join(server.args or []) or '-'}")
        return "\n".join(lines)
