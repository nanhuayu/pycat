"""Resource discovery; network and installation only follow an action."""
from __future__ import annotations

import asyncio
import threading
from html import escape

from PyQt6.QtCore import Qt, QThreadPool, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget, QTextBrowser,
)

from pycat.gui.runtime.background_job import BackgroundJob
from pycat.gui.settings.components import (
    RESOURCE_SUBTITLE_ROLE, RESOURCE_TITLE_ROLE, configure_settings_resource_list,
)
from pycat.gui.settings.components import SettingsListDetailLayout, RESOURCE_DESCRIPTION_ROLE
from pycat.gui.settings.page_header import build_page_header
from pycat.models.contracts.mcp import McpServerConfig


class DiscoveryPanel(QWidget):
    mcp_prepared = pyqtSignal(object)
    skills_changed = pyqtSignal()

    def __init__(self, *, kind, service, work_dir="", servers_provider=lambda: (), parent=None, show_header=True):
        super().__init__(parent)
        self._service, self._work_dir, self._servers = service, work_dir, servers_provider
        self._kind = kind
        self._rows, self._plans = [], {}
        self._market_rows = None
        self._market_query, self._next_cursor, self._installed_id = "", "", ""
        self._job = None
        self._cancel_event = None
        self._pending_skill = None
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        if show_header:
            root.addWidget(build_page_header("MCP 市场" if kind == "mcp" else "Skills 市场", "搜索公开目录，核对来源并按需安装。"))
        split = SettingsListDetailLayout()
        self.browser = split
        root.addWidget(split, 1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索 MCP" if kind == "mcp" else "搜索 Skills")
        self.search.setAccessibleName("市场搜索词")
        self.search.textChanged.connect(self._filter)
        self.search.returnPressed.connect(self._search_market)
        search_actions = QHBoxLayout()
        search_actions.addWidget(self.search, 1)
        self.search_button = QPushButton("搜索市场")
        self.search_button.clicked.connect(self._search_market)
        self.reset_button = QPushButton("推荐")
        self.reset_button.clicked.connect(self._reset_market)
        search_actions.addWidget(self.search_button)
        search_actions.addWidget(self.reset_button)
        split.toolbar_layout.addLayout(search_actions)
        self.list_widget = configure_settings_resource_list(QListWidget())
        self.list_widget.currentItemChanged.connect(self._selection)
        split.list_layout.addWidget(self.list_widget, 1)
        split.bind(self.list_widget)
        self.next_button = QPushButton("下一页")
        self.next_button.clicked.connect(lambda: self._search_market(next_page=True))
        self.next_button.hide()
        split.list_layout.addWidget(self.next_button)
        self.import_button = QPushButton("从 GitHub 添加")
        self.import_button.clicked.connect(self._import_skill)
        search_actions.addWidget(self.import_button)
        self.import_button.setVisible(kind == "skill")
        self.details = QTextBrowser()
        self.details.setObjectName("resource_preview")
        self.details.setFrameShape(QTextBrowser.Shape.NoFrame)
        self.details.setReadOnly(True)
        self.details.setAccessibleName("扩展详情")
        split.detail_layout.addWidget(self.details, 1)
        self.options_combo = QComboBox()
        self.options_combo.setAccessibleName("MCP 接入方式")
        self.options_combo.hide()
        split.detail_layout.addWidget(self.options_combo)
        actions = QHBoxLayout()
        self.source_button = QPushButton("查看来源")
        self.source_button.clicked.connect(self._open_source)
        self.check_button = QPushButton("检查更新")
        self.check_button.clicked.connect(self._check)
        self.install_button = QPushButton("安装")
        self.install_button.clicked.connect(self._install)
        for button in (self.source_button, self.check_button, self.install_button):
            actions.addWidget(button)
        actions.addStretch(1)
        split.detail_layout.insertLayout(1, actions)
        status_row = QHBoxLayout()
        self.status = QLabel("搜索市场或检查版本时才会联网。")
        self.status.setWordWrap(True)
        status_row.addWidget(self.status, 1)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.cancel_pending)
        self.cancel_button.hide()
        status_row.addWidget(self.cancel_button)
        root.addLayout(status_row)
        self.refresh()

    def refresh(self):
        selected = self._selected().get("id")
        try:
            servers = self._servers()
        except Exception as exc:
            servers = ()
            self.status.setText("MCP 草稿尚未完成：" + str(exc))
        catalog = self._service.catalog(work_dir=self._work_dir, servers=servers) if self._service else []
        self._rows = [row for row in catalog if row["kind"] == self._kind and (
            not row.get("installed") or row["id"] == "agent-browser" or row["id"] == self._installed_id)]
        if self._market_rows is not None:
            self._rows = list(self._market_rows)
        if self._pending_skill:
            self._rows = [row for row in self._rows if self._key(row) != self._key(self._pending_skill)]
            self._rows.append(self._pending_skill)
        self._filter()
        if selected:
            self.select_extension(selected)
        self.import_button.setEnabled(self._service is not None and self._job is None)
        self.search_button.setEnabled(self._service is not None and self._job is None)

    def _filter(self):
        self.list_widget.clear()
        query = self.search.text().casefold() if self._market_rows is None else ""
        for row in self._rows:
            if query not in " ".join(str(row.get(key, "")) for key in ("title", "source", "description")).casefold():
                continue
            item = QListWidgetItem(row["title"])
            item.setData(RESOURCE_TITLE_ROLE, row["title"])
            item.setData(RESOURCE_SUBTITLE_ROLE, row["status"] + " · " + row.get("source", ""))
            item.setData(RESOURCE_DESCRIPTION_ROLE, row["description"])
            item.setToolTip(row["title"] + "\n" + row["description"])
            item.setData(Qt.ItemDataRole.UserRole, row)
            self.list_widget.addItem(item)
        if self.list_widget.count():
            self.list_widget.setCurrentRow(0)
        else:
            self._selection()

    def select_extension(self, extension_id):
        self.search.clear()
        for i in range(self.list_widget.count()):
            if self.list_widget.item(i).data(Qt.ItemDataRole.UserRole)["id"] == extension_id:
                self.list_widget.setCurrentRow(i)
                return

    def show_installed(self, extension_id):
        self._market_rows = None
        self._installed_id = extension_id
        self.refresh()
        self.select_extension(extension_id)
        self.browser.show_detail()

    def _reset_market(self):
        self._market_rows = None
        self._installed_id = ""
        self._next_cursor = ""
        self.next_button.hide()
        self.search.clear()
        self.refresh()

    def _search_market(self, _checked=False, *, next_page=False):
        query = self._market_query if next_page else self.search.text()
        cursor = self._next_cursor if next_page else ""
        try:
            servers = self._servers()
        except Exception as exc:
            self.status.setText(str(exc))
            return
        def completed(result):
            self._market_query = query
            self._market_rows = result["items"]
            self._next_cursor = result["next_cursor"]
            self._pending_skill = None
            self.next_button.setVisible(bool(self._next_cursor))
            self.refresh()
            self.status.setText(f"找到 {len(self._market_rows)} 项。目录登记不代表 PyCat 已验证该程序；请核对来源与依赖。")
        self._start(lambda cancel: self._service.search_market(self._kind, query, cursor=cursor,
            work_dir=self._work_dir, servers=servers, cancelled=cancel), completed, "正在搜索公开市场…")

    def _selected(self):
        item = self.list_widget.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else {}

    @staticmethod
    def _key(row):
        return row.get("kind", ""), row.get("id", "")

    def _selection(self, *_):
        row = self._selected()
        plan = self._plans.get(self._key(row))
        ownership = {"bundled": "随 PyCat 更新；可在 Skills 页停用或复制后编辑。",
                     "managed": "PyCat 管理安装来源。主动检查更新；保留配置和本地修改。",
                     "external": "外部管理。请在原来源更新程序或重新导入技能；刷新工具列表不会升级程序。",
                     "directory": "查看在线目录，选定来源后按需配置或安装。",
                     "market": "公开市场条目。MCP 添加为停用草稿，填写凭据并测试后再启用；技能先核对仓库与固定提交。"}.get(row.get("management"), "")
        lines = [row.get("title", "请选择扩展"), row.get("description", ""), ownership]
        if row:
            lines += ["来源：" + row.get("source", ""), "当前版本：" + (str(row.get("current_version")) or "未记录")]
        if row.get("id") == "agent-browser" and row.get("kind") == "mcp":
            lines.append("本机浏览器：" + (row.get("browser_path") or "未找到；需先安装 Chrome / Edge / Chromium，或在 MCP 环境变量中指定程序。"))
            if not row.get("supported"):
                lines.append("此平台没有匹配的原生发布文件。")
        if plan:
            lines += ["可安装版本：" + plan["version"]]
            if plan.get("sha256"):
                lines += ["SHA-256：" + plan["sha256"]]
        if row.get("market_version"):
            lines.append("市场版本：" + row["market_version"])
        if row.get("options"):
            self.options_combo.blockSignals(True)
            self.options_combo.clear()
            for option in row["options"]:
                self.options_combo.addItem(option["label"], option["configuration"])
            self.options_combo.blockSignals(False)
        self.options_combo.setVisible(bool(row.get("options")))
        if row.get("management") == "market" and self._kind == "mcp" and not row.get("options"):
            lines.append("该条目需要自定义安装步骤，请查看来源后在已安装页手动添加配置。")
        self.details.setHtml(
            "".join("<p>" + escape(line).replace("\n", "<br>") + "</p>" for line in lines if line and line != row.get("title")))
        busy = self._job is not None
        for control in (self.search_button, self.reset_button, self.next_button):
            control.setEnabled(self._service is not None and not busy)
        self.source_button.setEnabled(bool(row.get("website")) and not busy)
        market_skill = row.get("management") == "market" and self._kind == "skill"
        self.check_button.setText("准备安装" if market_skill and not row.get("installed") else "检查更新")
        self.check_button.setEnabled((row.get("management") == "managed" or market_skill) and row.get("supported", True) and not busy)
        self.install_button.setText(("已添加" if row.get("installed") else "添加配置") if row.get("options") else "更新" if row.get("current_version") else "安装")
        self.install_button.setEnabled((bool(row.get("options")) and not row.get("installed") or bool(plan) and plan.get("version") != row.get("current_version")) and not busy)

    def _start(self, operation, completed, message):
        if self._job:
            return
        cancel = threading.Event()
        self._cancel_event = cancel

        async def run():
            task = asyncio.create_task(operation(cancel.is_set))
            try:
                while not task.done():
                    if cancel.is_set():
                        task.cancel()
                        raise InterruptedError("操作已取消。")
                    await asyncio.wait({task}, timeout=0.1)
                return await task
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        job = BackgroundJob(run)
        self._job = job
        self.destroyed.connect(lambda: (cancel.set(), job.abandon()))
        self.status.setText(message)
        self.cancel_button.show()
        self.list_widget.setEnabled(False)
        self.import_button.setEnabled(False)
        self._selection()

        def finished(result, error):
            if self._job is not job:
                return
            self._job = None
            self.cancel_button.hide()
            self.list_widget.setEnabled(True)
            self.import_button.setEnabled(True)
            if error:
                self.status.setText(str(error))
            else:
                try:
                    completed(result)
                except Exception as exc:
                    self.status.setText(str(exc))
            self._selection()

        job.signals.finished.connect(finished)
        QThreadPool.globalInstance().start(job)

    def cancel_pending(self):
        if self._cancel_event:
            self._cancel_event.set()
        if self._job:
            self._job.abandon()
            self._job = None
            self.status.setText("已请求取消。正在执行的下载将停止，原有配置保持不变。")
        self.cancel_button.hide()
        self.list_widget.setEnabled(True)
        self.import_button.setEnabled(self._service is not None)
        self._selection()

    def hideEvent(self, event):
        self.cancel_pending()
        super().hideEvent(event)

    def showEvent(self, event):
        self.refresh()
        super().showEvent(event)

    def _check(self):
        row = dict(self._selected())
        def completed(plan):
            self._plans[self._key(row)] = plan
            self.status.setText("已是最新版本。" if plan["version"] == row.get("current_version") else "版本已确认，点击安装或更新以继续。")
        operation = (lambda cancel: self._service.check_browser(cancelled=cancel)) if row["kind"] == "mcp" else (
            lambda cancel: self._service.check_skill(row["id"], work_dir=self._work_dir, cancelled=cancel))
        if row.get("management") == "market":
            operation = (lambda cancel: self._service.check_skill(row["skill_name"], work_dir=self._work_dir, cancelled=cancel)) if row.get("installed") else (
                lambda cancel: self._service.preview_market_skill(row["repository"], row["skill_name"], cancelled=cancel))
        self._start(operation, completed, "正在检查官方来源…")

    def _install(self):
        row = dict(self._selected())
        if row.get("options"):
            try:
                config = McpServerConfig.from_dict(self.options_combo.currentData())
                if any(item.name == config.name for item in self._servers()):
                    raise ValueError("已存在同名 MCP，请在已安装页查看或修改名称。")
                self.mcp_prepared.emit(config)
                for stored in self._market_rows or []:
                    if self._key(stored) == self._key(row):
                        stored.update(installed=True, status="已配置")
                self.refresh()
                self.status.setText("已加入停用草稿；在已安装页填写凭据、测试连接并启用，保存设置后生效。")
            except Exception as exc:
                self.status.setText(str(exc))
            return
        plan = dict(self._plans[self._key(row)])
        if row["kind"] == "mcp":
            try:
                servers = self._servers()
                existing = next((s for s in servers if s.integration == "agent-browser"), None)
                if existing is None and any(s.name == "browser" for s in servers):
                    raise ValueError("已存在名为 browser 的 MCP，请先重命名该服务。")
            except Exception as exc:
                self.status.setText(str(exc))
                return
            def completed(config):
                self.mcp_prepared.emit(config)
                self.refresh()
                self.status.setText("驱动已验证，MCP 配置已加入草稿。保存设置后生效。")
            self._start(lambda cancel: self._service.prepare_browser(plan, existing=existing, cancelled=cancel),
                        completed, "正在下载、校验并测试浏览器 MCP…")
        else:
            def completed(_):
                self._pending_skill = None
                for stored in self._market_rows or []:
                    if self._key(stored) == self._key(row):
                        stored.update(installed=True, current_version=plan["version"], status="已安装")
                self.skills_changed.emit()
                self.refresh()
                self.status.setText("技能已安装并生效，来源和提交已记录。")
            self._start(lambda cancel: self._service.install_skill(plan, scope=plan.get("scope", "global"),
                work_dir=self._work_dir, overwrite=bool(row.get("current_version")), cancelled=cancel),
                completed, "正在下载并验证技能…")

    def _open_source(self):
        url = self._selected().get("website", "")
        if url.startswith("https://"):
            QDesktopServices.openUrl(QUrl(url))

    def _import_skill(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("从 GitHub 安装 Skill")
        layout = QFormLayout(dialog)
        repository, path, ref = QLineEdit(), QLineEdit(), QLineEdit("HEAD")
        repository.setPlaceholderText("owner/repo")
        path.setPlaceholderText("skills/example-skill")
        scope = QComboBox()
        scope.addItem("用户技能", "global")
        if self._work_dir:
            scope.addItem("当前项目", "project")
        for label, field in (("仓库", repository), ("技能目录", path), ("分支 / 标签 / 提交", ref), ("安装到", scope)):
            layout.addRow(label, field)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        repo, directory, reference, selected_scope = repository.text(), path.text(), ref.text(), scope.currentData()
        def completed(plan):
            plan["scope"] = selected_scope
            self._pending_skill = {"id": plan["name"], "kind": "skill", "title": plan["name"], "management": "managed",
                "source": plan["repository"], "website": "https://github.com/" + plan["repository"],
                "description": plan["path"], "status": "待安装", "current_version": ""}
            self._plans[("skill", plan["name"])] = plan
            self.refresh()
            self.select_extension(plan["name"])
            self.browser.show_detail()
            self.status.setText("已固定提交，请核对来源后点击安装。")
        self._start(lambda cancel: self._service.preview_skill(repo, directory, reference, cancelled=cancel),
                    completed, "正在检查技能来源…")
