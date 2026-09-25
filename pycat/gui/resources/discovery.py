"""Resource discovery; network and installation only follow an action."""
from __future__ import annotations

import asyncio
import threading
from html import escape
from pathlib import Path

from PyQt6.QtCore import QCoreApplication, Qt, QThreadPool, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pycat.gui.runtime.background_job import BackgroundJob
from pycat.gui.settings.components import (
    RESOURCE_DESCRIPTION_ROLE,
    RESOURCE_SUBTITLE_ROLE,
    RESOURCE_TITLE_ROLE,
    SettingsListDetailLayout,
    build_dialog_button_box,
    configure_settings_resource_list,
)
from pycat.gui.settings.page_header import build_page_header
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.theme import configure_icon_button
from pycat.gui.view_models.extension_labels import extension_text
from pycat.gui.widgets.themed_line_edit import ThemedLineEdit, ThemedTextBrowser
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
        self._description_key = None
        self._catalog_loaded = False
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        if show_header:
            root.addWidget(build_page_header(QCoreApplication.translate('DiscoveryPanel', '发现 MCP') if kind == "mcp" else QCoreApplication.translate('DiscoveryPanel', '发现技能'), QCoreApplication.translate('DiscoveryPanel', '搜索公开目录，核对来源并按需安装。')))
        split = SettingsListDetailLayout()
        self.browser = split
        root.addWidget(split, 1)
        self.search = ThemedLineEdit()
        self.search.setPlaceholderText(QCoreApplication.translate('DiscoveryPanel', '搜索 MCP') if kind == "mcp" else QCoreApplication.translate('DiscoveryPanel', '搜索技能'))
        self.search.setAccessibleName(QCoreApplication.translate('DiscoveryPanel', '市场搜索词'))
        self.search.textChanged.connect(self._filter)
        self.search.returnPressed.connect(self._search_market)
        search_actions = QHBoxLayout()
        search_actions.addWidget(self.search, 1)
        self.search_button = QPushButton(QCoreApplication.translate('DiscoveryPanel', '搜索市场'))
        self.search_button.clicked.connect(self._search_market)
        self.reset_button = QPushButton(QCoreApplication.translate('DiscoveryPanel', '推荐'))
        self.reset_button.clicked.connect(self._reset_market)
        search_actions.addWidget(self.search_button)
        search_actions.addWidget(self.reset_button)
        split.toolbar_layout.addLayout(search_actions)
        self.list_widget = configure_settings_resource_list(QListWidget())
        self.list_widget.currentItemChanged.connect(self._selection)
        split.list_layout.addWidget(self.list_widget, 1)
        split.bind(self.list_widget)
        self.next_button = QPushButton(QCoreApplication.translate('DiscoveryPanel', '下一页'))
        self.next_button.clicked.connect(lambda: self._search_market(next_page=True))
        self.next_button.hide()
        split.list_layout.addWidget(self.next_button)
        self.import_button = QPushButton(QCoreApplication.translate('DiscoveryPanel', '从 GitHub 添加'))
        self.import_button.clicked.connect(self._import_skill)
        search_actions.addWidget(self.import_button)
        self.import_button.setVisible(kind == "skill")
        self.details = ThemedTextBrowser()
        self.details.setObjectName("resource_preview")
        self.details.setFrameShape(ThemedTextBrowser.Shape.NoFrame)
        self.details.setReadOnly(True)
        self.details.setAccessibleName(QCoreApplication.translate('DiscoveryPanel', '扩展详情'))
        split.detail_layout.addWidget(self.details, 1)
        self.description_button = QPushButton(QCoreApplication.translate('DiscoveryPanel', '展开说明'))
        self.description_button.setCheckable(True)
        self.description_button.toggled.connect(self._selection)
        self.options_combo = QComboBox()
        self.options_combo.setAccessibleName(QCoreApplication.translate('DiscoveryPanel', 'MCP 接入方式'))
        self.options_combo.hide()
        split.detail_layout.addWidget(self.options_combo)
        actions = QHBoxLayout()
        self.source_button = QToolButton()
        configure_icon_button(self.source_button, Icons.get(Icons.EXTERNAL_OPEN), self.tr("打开来源网站"))
        self.source_button.clicked.connect(self._open_source)
        self.directory_button = QToolButton()
        configure_icon_button(self.directory_button, Icons.get(Icons.FOLDER), self.tr("打开目录"))
        self.directory_button.clicked.connect(self._open_directory)
        self.check_button = QPushButton(QCoreApplication.translate('DiscoveryPanel', '检查更新'))
        self.check_button.clicked.connect(self._check)
        self.install_button = QPushButton(QCoreApplication.translate('DiscoveryPanel', '安装'))
        self.install_button.clicked.connect(self._install)
        for button in (self.source_button, self.directory_button, self.check_button, self.install_button):
            actions.addWidget(button)
        actions.addStretch(1)
        actions.addWidget(self.description_button)
        split.detail_layout.insertLayout(1, actions)
        status_row = QHBoxLayout()
        self.status = QLabel(QCoreApplication.translate('DiscoveryPanel', '搜索市场或检查版本时才会联网。'))
        self.status.setProperty("muted", True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        status_row.addWidget(self.status, 1)
        self.cancel_button = QPushButton(QCoreApplication.translate('DiscoveryPanel', '取消'))
        self.cancel_button.clicked.connect(self.cancel_pending)
        self.cancel_button.hide()
        status_row.addWidget(self.cancel_button)
        root.addLayout(status_row)
        self._selection()
        self.import_button.setEnabled(service is not None)
        self.search_button.setEnabled(service is not None)

    def ensure_loaded(self):
        if not self._catalog_loaded:
            self.refresh()

    def refresh(self):
        self._catalog_loaded = True
        selected = self._selected().get("id")
        try:
            servers = self._servers()
        except Exception as exc:
            servers = ()
            self.status.setText(QCoreApplication.translate('DiscoveryPanel', 'MCP 草稿尚未完成：') + str(exc))
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
            display = {key: extension_text(row, key) for key in ("title", "source", "description", "status")}
            searchable = [str(row.get(key, "")) for key in ("title", "source", "description")]
            if query not in " ".join([*searchable, *display.values()]).casefold():
                continue
            item = QListWidgetItem(display["title"])
            item.setData(RESOURCE_TITLE_ROLE, display["title"])
            item.setData(RESOURCE_SUBTITLE_ROLE, display["status"] + " · " + display["source"])
            item.setData(RESOURCE_DESCRIPTION_ROLE, display["description"])
            item.setToolTip(display["title"] + "\n" + display["description"])
            item.setData(Qt.ItemDataRole.UserRole, row)
            self.list_widget.addItem(item)
        if self.list_widget.count():
            self.list_widget.setCurrentRow(0)
        else:
            self._selection()

    def select_extension(self, extension_id):
        self.ensure_loaded()
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
            self.status.setText(QCoreApplication.translate('DiscoveryPanel', '找到 {value} 项。目录登记不代表 PyCat 已验证该程序；请核对来源与依赖。').format(value=len(self._market_rows)))
        self._start(lambda cancel: self._service.search_market(self._kind, query, cursor=cursor,
            work_dir=self._work_dir, servers=servers, cancelled=cancel), completed, QCoreApplication.translate('DiscoveryPanel', '正在搜索公开市场…'))

    def _selected(self):
        item = self.list_widget.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else {}

    @staticmethod
    def _key(row):
        return row.get("kind", ""), row.get("id", "")

    def _selection(self, *_):
        row = self._selected()
        plan = self._plans.get(self._key(row)) if row.get("management") in {"managed", "market"} else None
        ownership = {"bundled": QCoreApplication.translate('DiscoveryPanel', '随 PyCat 更新。可在已安装页停用，或复制后编辑。'),
                     "managed": QCoreApplication.translate('DiscoveryPanel', '由 PyCat 管理。检查更新后选择安装版本。'),
                     "external": QCoreApplication.translate('DiscoveryPanel', '外部管理，请通过原安装方式更新。'),
                     "directory": QCoreApplication.translate('DiscoveryPanel', '打开目录，选择需要的资源。'),
                     "market": QCoreApplication.translate('DiscoveryPanel', '添加后先填写凭据并测试，再启用。') if self._kind == "mcp" else QCoreApplication.translate('DiscoveryPanel', '准备安装时核对仓库与固定提交。')}.get(row.get("management"), "")
        key = self._key(row)
        if key != self._description_key:
            self._description_key = key
            self.description_button.blockSignals(True)
            self.description_button.setChecked(False)
            self.description_button.blockSignals(False)
        description = extension_text(row, "description")
        self.description_button.setVisible(len(description) > 280)
        expanded = self.description_button.isChecked()
        self.description_button.setText(QCoreApplication.translate('DiscoveryPanel', '收起说明') if expanded else QCoreApplication.translate('DiscoveryPanel', '展开说明'))
        if not expanded and len(description) > 280:
            description = description[:280].rstrip() + "…"
        lines = [description, ownership]
        if row:
            lines += [QCoreApplication.translate('DiscoveryPanel', '来源：') + extension_text(row, "source")]
            if row.get("installed"):
                lines += [QCoreApplication.translate('DiscoveryPanel', '状态：') + extension_text(row, "status"), QCoreApplication.translate('DiscoveryPanel', '当前版本：') + str(row.get("current_version") or QCoreApplication.translate('DiscoveryPanel', '未记录'))]
        if row.get("id") == "agent-browser" and row.get("kind") == "mcp":
            lines.append(QCoreApplication.translate('DiscoveryPanel', '本机浏览器：') + (row.get("browser_path") or QCoreApplication.translate('DiscoveryPanel', '未找到；需先安装 Chrome / Edge / Chromium，或在 MCP 环境变量中指定程序。')))
            if not row.get("supported"):
                lines.append(QCoreApplication.translate('DiscoveryPanel', '此平台没有匹配的原生发布文件。'))
        if plan:
            lines += [QCoreApplication.translate('DiscoveryPanel', '可安装版本：') + plan["version"]]
            if plan.get("sha256"):
                lines += ["SHA-256：" + plan["sha256"]]
        if row.get("market_version"):
            lines.append(QCoreApplication.translate('DiscoveryPanel', '市场版本：') + row["market_version"])
        if row.get("options"):
            self.options_combo.blockSignals(True)
            self.options_combo.clear()
            for option in row["options"]:
                self.options_combo.addItem(option["label"], option["configuration"])
            self.options_combo.blockSignals(False)
        self.options_combo.setVisible(bool(row.get("options")))
        if row.get("management") == "market" and self._kind == "mcp" and not row.get("options"):
            lines.append(QCoreApplication.translate('DiscoveryPanel', '该条目需要自定义安装步骤，请查看来源后在已安装页手动添加配置。'))
        self.details.setHtml(
            "".join("<p>" + escape(line).replace("\n", "<br>") + "</p>" for line in lines if line and line != row.get("title")))
        busy = self._job is not None
        for control in (self.search_button, self.reset_button, self.next_button):
            control.setEnabled(self._service is not None and not busy)
        self.source_button.setVisible(bool(row.get("website")))
        self.source_button.setEnabled(not busy)
        local_skill = self._kind == "skill" and row.get("installed") and row.get("management") in {"bundled", "managed", "external"}
        self.directory_button.setVisible(bool(local_skill))
        self.directory_button.setEnabled(self._service is not None and not busy)
        market_skill = row.get("management") == "market" and self._kind == "skill"
        self.check_button.setText(QCoreApplication.translate('DiscoveryPanel', '准备安装') if market_skill and not row.get("installed") else QCoreApplication.translate('DiscoveryPanel', '检查更新'))
        checkable = row.get("management") == "managed" or market_skill
        self.check_button.setVisible(checkable)
        self.check_button.setEnabled(checkable and row.get("supported", True) and not busy and self._service is not None)
        self.check_button.setToolTip(QCoreApplication.translate('DiscoveryPanel', '当前平台没有可用的发布文件') if not row.get("supported", True) else "")
        self.install_button.setText((QCoreApplication.translate('DiscoveryPanel', '已添加') if row.get("installed") else QCoreApplication.translate('DiscoveryPanel', '添加配置')) if row.get("options") else QCoreApplication.translate('DiscoveryPanel', '更新') if row.get("current_version") else QCoreApplication.translate('DiscoveryPanel', '安装'))
        self.install_button.setVisible(bool(row.get("options") or plan))
        self.install_button.setEnabled((bool(row.get("options")) and not row.get("installed") or bool(plan) and plan.get("version") != row.get("current_version")) and not busy)

    def _open_directory(self):
        row = self._selected()
        if not (self._service and self._kind == "skill" and row.get("installed")
                and row.get("management") in {"bundled", "managed", "external"}):
            return
        # Resolve the current installation through its owner, never a market path.
        try:
            skill = self._service.skills.get(row["id"], work_dir=self._work_dir, include_disabled=True)
            source = Path(skill.source) if skill and skill.source else None
            if source is None or not source.is_file():
                self.status.setText(QCoreApplication.translate('DiscoveryPanel', '技能来源已不存在，请刷新列表。'))
            elif not QDesktopServices.openUrl(QUrl.fromLocalFile(str(source.resolve().parent))):
                self.status.setText(QCoreApplication.translate('DiscoveryPanel', '无法打开技能目录。'))
        except (OSError, ValueError) as exc:
            self.status.setText(QCoreApplication.translate('DiscoveryPanel', '无法定位技能来源：{exc}').format(exc=exc))

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
                        raise InterruptedError(QCoreApplication.translate('DiscoveryPanel', '操作已取消。'))
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
            self.status.setText(QCoreApplication.translate('DiscoveryPanel', '已请求取消。正在执行的下载将停止，原有配置保持不变。'))
        self.cancel_button.hide()
        self.list_widget.setEnabled(True)
        self.import_button.setEnabled(self._service is not None)
        self._selection()

    def hideEvent(self, event):
        self.cancel_pending()
        self._catalog_loaded = False
        super().hideEvent(event)

    def showEvent(self, event):
        self.ensure_loaded()
        super().showEvent(event)

    def _check(self):
        row = dict(self._selected())
        def completed(plan):
            self._plans[self._key(row)] = plan
            self.status.setText(QCoreApplication.translate('DiscoveryPanel', '已是最新版本。') if plan["version"] == row.get("current_version") else QCoreApplication.translate('DiscoveryPanel', '版本已确认，点击安装或更新以继续。'))
        operation = (lambda cancel: self._service.check_browser(cancelled=cancel)) if row["kind"] == "mcp" else (
            lambda cancel: self._service.check_skill(row["id"], work_dir=self._work_dir, cancelled=cancel))
        if row.get("management") == "market":
            operation = (lambda cancel: self._service.check_skill(row["skill_name"], work_dir=self._work_dir, cancelled=cancel)) if row.get("installed") else (
                lambda cancel: self._service.preview_market_skill(row["repository"], row["skill_name"], cancelled=cancel))
        self._start(operation, completed, QCoreApplication.translate('DiscoveryPanel', '正在检查官方来源…'))

    def _install(self):
        row = dict(self._selected())
        if row.get("options"):
            try:
                config = McpServerConfig.from_dict(self.options_combo.currentData())
                if any(item.name == config.name for item in self._servers()):
                    raise ValueError(QCoreApplication.translate('DiscoveryPanel', '已存在同名 MCP，请在已安装页查看或修改名称。'))
                self.mcp_prepared.emit(config)
                for stored in self._market_rows or []:
                    if self._key(stored) == self._key(row):
                        stored.update(installed=True, status="已配置")
                self.refresh()
                self.status.setText(QCoreApplication.translate('DiscoveryPanel', '已加入停用草稿；在已安装页填写凭据、测试连接并启用，保存设置后生效。'))
            except Exception as exc:
                self.status.setText(str(exc))
            return
        plan = dict(self._plans[self._key(row)])
        if row["kind"] == "mcp":
            try:
                servers = self._servers()
                existing = next((s for s in servers if s.integration == "agent-browser"), None)
                if existing is None and any(s.name == "browser" for s in servers):
                    raise ValueError(QCoreApplication.translate('DiscoveryPanel', '已存在名为 browser 的 MCP，请先重命名该服务。'))
            except Exception as exc:
                self.status.setText(str(exc))
                return
            def completed(config):
                self.mcp_prepared.emit(config)
                self.refresh()
                self.status.setText(QCoreApplication.translate('DiscoveryPanel', '驱动已验证，MCP 配置已加入草稿。保存设置后生效。'))
            self._start(lambda cancel: self._service.prepare_browser(plan, existing=existing, cancelled=cancel),
                        completed, QCoreApplication.translate('DiscoveryPanel', '正在下载、校验并测试浏览器 MCP…'))
        else:
            def completed(_):
                self._pending_skill = None
                for stored in self._market_rows or []:
                    if self._key(stored) == self._key(row):
                        stored.update(installed=True, current_version=plan["version"], status="已安装")
                self.skills_changed.emit()
                self.refresh()
                self.status.setText(QCoreApplication.translate('DiscoveryPanel', '技能已安装并生效，来源和提交已记录。'))
            self._start(lambda cancel: self._service.install_skill(plan, scope=plan.get("scope", "global"),
                work_dir=self._work_dir, overwrite=bool(row.get("current_version")), cancelled=cancel),
                completed, QCoreApplication.translate('DiscoveryPanel', '正在下载并验证技能…'))

    def _open_source(self):
        url = self._selected().get("website", "")
        if url.startswith("https://"):
            QDesktopServices.openUrl(QUrl(url))

    def _import_skill(self):
        dialog = QDialog(self)
        dialog.setWindowTitle(QCoreApplication.translate('DiscoveryPanel', '从 GitHub 添加技能'))
        layout = QFormLayout(dialog)
        repository, path, ref = ThemedLineEdit(), ThemedLineEdit(), ThemedLineEdit("HEAD")
        repository.setPlaceholderText("owner/repo")
        path.setPlaceholderText("skills/example-skill")
        scope = QComboBox()
        scope.addItem(QCoreApplication.translate('DiscoveryPanel', '用户技能'), "global")
        if self._work_dir:
            scope.addItem(QCoreApplication.translate('DiscoveryPanel', '当前项目'), "project")
        for label, field in ((QCoreApplication.translate('DiscoveryPanel', '仓库'), repository), (QCoreApplication.translate('DiscoveryPanel', '技能目录'), path), (QCoreApplication.translate('DiscoveryPanel', '分支 / 标签 / 提交'), ref), (QCoreApplication.translate('DiscoveryPanel', '安装到'), scope)):
            layout.addRow(label, field)
        buttons = build_dialog_button_box(dialog, accept_text=QCoreApplication.translate('DiscoveryPanel', '准备安装'), accept_button=QDialogButtonBox.StandardButton.Ok)
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
            self.status.setText(QCoreApplication.translate('DiscoveryPanel', '已固定提交，请核对来源后点击安装。'))
        self._start(lambda cancel: self._service.preview_skill(repo, directory, reference, cancelled=cancel),
                    completed, QCoreApplication.translate('DiscoveryPanel', '正在检查技能来源…'))
