"""Shared extension details and cancellable prepare/install actions."""
from __future__ import annotations

import asyncio
import threading
from html import escape
from pathlib import Path

from PyQt6.QtCore import QCoreApplication, Qt, QThreadPool, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QToolButton, QVBoxLayout, QWidget

from pycat.gui.runtime.background_job import BackgroundJob
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.theme import configure_icon_button
from pycat.gui.view_models.extension_labels import extension_text
from pycat.gui.widgets.themed_line_edit import ThemedTextBrowser
from pycat.models.contracts.mcp import McpServerConfig


class ExtensionDetails(QWidget):
    mcp_prepared = pyqtSignal(object)
    skills_changed = pyqtSignal()
    changed = pyqtSignal(object)
    extension_prepared = pyqtSignal(object)
    busy_changed = pyqtSignal(bool)

    def __init__(self, *, kind, service, work_dir="", servers_provider=lambda: (), parent=None):
        super().__init__(parent)
        self._kind, self._service, self._work_dir, self._servers = kind, service, work_dir, servers_provider
        self._row, self._plans = {}, {}
        self._job = self._cancel_event = self._description_key = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        self.details = ThemedTextBrowser()
        self.details.setObjectName("resource_preview")
        self.details.setFrameShape(ThemedTextBrowser.Shape.NoFrame)
        self.details.setReadOnly(True)
        self.details.setAccessibleName(QCoreApplication.translate('ExtensionDetails', '扩展详情'))
        root.addWidget(self.details, 1)
        self.description_button = QPushButton(QCoreApplication.translate('ExtensionDetails', '展开说明'))
        self.description_button.setCheckable(True)
        self.description_button.toggled.connect(self._selection)
        self.options_combo = QComboBox()
        self.options_combo.setAccessibleName(QCoreApplication.translate('ExtensionDetails', 'MCP 接入方式'))
        self.options_combo.hide()
        root.addWidget(self.options_combo)
        actions = QHBoxLayout()
        self.source_button = QToolButton()
        configure_icon_button(self.source_button, Icons.get(Icons.EXTERNAL_OPEN), QCoreApplication.translate("ExtensionDetails", "打开来源网站"))
        self.source_button.clicked.connect(self._open_source)
        self.directory_button = QToolButton()
        configure_icon_button(self.directory_button, Icons.get(Icons.FOLDER), QCoreApplication.translate("ExtensionDetails", "打开目录"))
        self.directory_button.clicked.connect(self._open_directory)
        self.check_button = QPushButton(QCoreApplication.translate('ExtensionDetails', '检查更新'))
        self.check_button.clicked.connect(self._check)
        self.install_button = QPushButton(QCoreApplication.translate('ExtensionDetails', '安装'))
        self.install_button.clicked.connect(self._install)
        for button in (self.source_button, self.directory_button, self.check_button, self.install_button):
            actions.addWidget(button)
        actions.addStretch(1)
        actions.addWidget(self.description_button)
        root.insertLayout(0, actions)
        self.status_bar = QWidget(self)
        status_row = QHBoxLayout(self.status_bar)
        status_row.setContentsMargins(0, 0, 0, 0)
        self.status = QLabel(QCoreApplication.translate('ExtensionDetails', '搜索市场或检查版本时才会联网。'))
        self.status.setProperty("muted", True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        status_row.addWidget(self.status, 1)
        self.cancel_button = QPushButton(QCoreApplication.translate('ExtensionDetails', '取消'))
        self.cancel_button.clicked.connect(self.cancel_pending)
        self.cancel_button.hide()
        status_row.addWidget(self.cancel_button)
        self._selection()

    def set_extension(self, row):
        self._row = dict(row or {})
        self._selection()

    def _selected(self):
        return self._row

    @staticmethod
    def _key(row):
        return row.get("kind", ""), row.get("id", "")

    def _selection(self, *_):
        row = self._selected()
        plan = self._plans.get(self._key(row)) if row.get("management") in {"managed", "market"} else None
        ownership = {"bundled": QCoreApplication.translate('ExtensionDetails', '随 PyCat 更新。可在技能列表停用，或复制后编辑。'),
                     "managed": QCoreApplication.translate('ExtensionDetails', '由 PyCat 管理。检查更新后选择安装版本。'),
                     "external": QCoreApplication.translate('ExtensionDetails', '外部管理，请通过原安装方式更新。'),
                     "directory": QCoreApplication.translate('ExtensionDetails', '打开目录，选择需要的资源。'),
                     "market": QCoreApplication.translate('ExtensionDetails', '添加后先填写凭据并测试，再启用。') if self._kind == "mcp" else QCoreApplication.translate('ExtensionDetails', '准备安装时核对仓库与固定提交。')}.get(row.get("management"), "")
        key = self._key(row)
        if key != self._description_key:
            self._description_key = key
            self.description_button.blockSignals(True)
            self.description_button.setChecked(False)
            self.description_button.blockSignals(False)
        description = extension_text(row, "description")
        self.description_button.setVisible(len(description) > 280)
        expanded = self.description_button.isChecked()
        self.description_button.setText(QCoreApplication.translate('ExtensionDetails', '收起说明') if expanded else QCoreApplication.translate('ExtensionDetails', '展开说明'))
        if not expanded and len(description) > 280:
            description = description[:280].rstrip() + "…"
        lines = [description, ownership]
        if row:
            lines += [QCoreApplication.translate('ExtensionDetails', '来源：') + extension_text(row, "source")]
            if row.get("installed"):
                lines += [QCoreApplication.translate('ExtensionDetails', '状态：') + extension_text(row, "status"), QCoreApplication.translate('ExtensionDetails', '当前版本：') + str(row.get("current_version") or QCoreApplication.translate('ExtensionDetails', '未记录'))]
        if row.get("id") == "agent-browser" and row.get("kind") == "mcp":
            lines.append(QCoreApplication.translate('ExtensionDetails', '本机浏览器：') + (row.get("browser_path") or QCoreApplication.translate('ExtensionDetails', '未找到；需先安装 Chrome / Edge / Chromium，或在 MCP 环境变量中指定程序。')))
            if not row.get("supported"):
                lines.append(QCoreApplication.translate('ExtensionDetails', '此平台没有匹配的原生发布文件。'))
        if plan:
            lines += [QCoreApplication.translate('ExtensionDetails', '可安装版本：') + plan["version"]]
            if plan.get("sha256"):
                lines += ["SHA-256：" + plan["sha256"]]
        if row.get("market_version"):
            lines.append(QCoreApplication.translate('ExtensionDetails', '市场版本：') + row["market_version"])
        if row.get("options"):
            self.options_combo.blockSignals(True)
            self.options_combo.clear()
            for option in row["options"]:
                self.options_combo.addItem(option["label"], option["configuration"])
            self.options_combo.blockSignals(False)
        self.options_combo.setVisible(bool(row.get("options")))
        if row.get("management") == "market" and self._kind == "mcp" and not row.get("options"):
            lines.append(QCoreApplication.translate('ExtensionDetails', '该条目需要自定义安装步骤，请查看来源后通过“添加”手动配置。'))
        self.details.setHtml(
            "".join("<p>" + escape(line).replace("\n", "<br>") + "</p>" for line in lines if line and line != row.get("title")))
        busy = self._job is not None
        self.source_button.setVisible(bool(row.get("website")))
        self.source_button.setEnabled(not busy)
        local_skill = self._kind == "skill" and row.get("installed") and row.get("management") in {"bundled", "managed", "external"}
        self.directory_button.setVisible(bool(local_skill))
        self.directory_button.setEnabled(self._service is not None and not busy)
        market_skill = row.get("management") == "market" and self._kind == "skill"
        self.check_button.setText(QCoreApplication.translate('ExtensionDetails', '准备安装') if market_skill and not row.get("installed") else QCoreApplication.translate('ExtensionDetails', '检查更新'))
        checkable = (row.get("management") == "managed" and (self._kind == "mcp" or row.get("installed"))) or market_skill
        self.check_button.setVisible(checkable)
        self.check_button.setEnabled(checkable and row.get("supported", True) and not busy and self._service is not None)
        self.check_button.setToolTip(QCoreApplication.translate('ExtensionDetails', '当前平台没有可用的发布文件') if not row.get("supported", True) else "")
        self.install_button.setText((QCoreApplication.translate('ExtensionDetails', '已添加') if row.get("installed") else QCoreApplication.translate('ExtensionDetails', '添加配置')) if row.get("options") else QCoreApplication.translate('ExtensionDetails', '更新') if row.get("current_version") else QCoreApplication.translate('ExtensionDetails', '安装'))
        self.install_button.setVisible(bool(row.get("options") or plan))
        self.install_button.setEnabled((bool(row.get("options")) and not row.get("installed") or bool(plan) and plan.get("version") != row.get("current_version")) and not busy)

    def _open_directory(self):
        row = self._selected()
        if not (self._service and self._kind == "skill" and row.get("installed")
                and row.get("management") in {"bundled", "managed", "external"}):
            return
        # Resolve the current installation through its owner, never a market path.
        try:
            skill = self._service.skills.get(row.get("skill_name") or row["id"], work_dir=self._work_dir, include_disabled=True)
            source = Path(skill.source) if skill and skill.source else None
            if source is None or not source.is_file():
                self.status.setText(QCoreApplication.translate('ExtensionDetails', '技能来源已不存在，请刷新列表。'))
            elif not QDesktopServices.openUrl(QUrl.fromLocalFile(str(source.resolve().parent))):
                self.status.setText(QCoreApplication.translate('ExtensionDetails', '无法打开技能目录。'))
        except (OSError, ValueError) as exc:
            self.status.setText(QCoreApplication.translate('ExtensionDetails', '无法定位技能来源：{exc}').format(exc=exc))

    def run_operation(self, operation, completed, message):
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
                        raise InterruptedError(QCoreApplication.translate('ExtensionDetails', '操作已取消。'))
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
        self.busy_changed.emit(True)
        self._selection()

        def finished(result, error):
            if self._job is not job:
                return
            self._job = None
            self.cancel_button.hide()
            self.busy_changed.emit(False)
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
            self.status.setText(QCoreApplication.translate('ExtensionDetails', '已请求取消。正在执行的下载将停止，原有配置保持不变。'))
        self.cancel_button.hide()
        self.busy_changed.emit(False)
        self._selection()

    def _check(self):
        row = dict(self._selected())
        def completed(plan):
            self._plans[self._key(row)] = plan
            self.status.setText(QCoreApplication.translate('ExtensionDetails', '已是最新版本。') if plan["version"] == row.get("current_version") else QCoreApplication.translate('ExtensionDetails', '版本已确认，点击安装或更新以继续。'))
        operation = (lambda cancel: self._service.check_browser(cancelled=cancel)) if row["kind"] == "mcp" else (
            lambda cancel: self._service.check_skill(row["id"], work_dir=self._work_dir, cancelled=cancel))
        if row.get("management") == "market":
            operation = (lambda cancel: self._service.check_skill(row["skill_name"], work_dir=self._work_dir, cancelled=cancel)) if row.get("installed") else (
                lambda cancel: self._service.preview_market_skill(row["repository"], row["skill_name"], cancelled=cancel))
        self.run_operation(operation, completed, QCoreApplication.translate('ExtensionDetails', '正在检查官方来源…'))

    def _install(self):
        row = dict(self._selected())
        if row.get("options"):
            try:
                config = McpServerConfig.from_dict(self.options_combo.currentData())
                if any(item.name == config.name for item in self._servers()):
                    raise ValueError(QCoreApplication.translate('ExtensionDetails', '已存在同名 MCP，请在列表中查看或修改名称。'))
                self.mcp_prepared.emit(config)
                row.update(installed=True, status="已配置")
                self.set_extension(row)
                self.changed.emit(row)
                self.status.setText(QCoreApplication.translate('ExtensionDetails', '已加入停用草稿；填写凭据、测试连接并启用，保存设置后生效。'))
            except Exception as exc:
                self.status.setText(str(exc))
            return
        plan = dict(self._plans[self._key(row)])
        if row["kind"] == "mcp":
            try:
                servers = self._servers()
                existing = next((s for s in servers if s.integration == "agent-browser"), None)
                if existing is None and any(s.name == "browser" for s in servers):
                    raise ValueError(QCoreApplication.translate('ExtensionDetails', '已存在名为 browser 的 MCP，请先重命名该服务。'))
            except Exception as exc:
                self.status.setText(str(exc))
                return
            def completed(config):
                self.mcp_prepared.emit(config)
                self.changed.emit(row)
                self.status.setText(QCoreApplication.translate('ExtensionDetails', '驱动已验证，MCP 配置已加入草稿。保存设置后生效。'))
            self.run_operation(lambda cancel: self._service.prepare_browser(plan, existing=existing, cancelled=cancel),
                        completed, QCoreApplication.translate('ExtensionDetails', '正在下载、校验并测试浏览器 MCP…'))
        else:
            def completed(_):
                row.update(installed=True, current_version=plan["version"], status="已安装")
                self.set_extension(row)
                self.changed.emit(row)
                self.skills_changed.emit()
                self.status.setText(QCoreApplication.translate('ExtensionDetails', '技能已安装并生效，来源和提交已记录。'))
            self.run_operation(lambda cancel: self._service.install_skill(plan, scope=plan.get("scope", "global"),
                work_dir=self._work_dir, overwrite=bool(row.get("current_version")), cancelled=cancel),
                completed, QCoreApplication.translate('ExtensionDetails', '正在下载并验证技能…'))

    def _open_source(self):
        url = self._selected().get("website", "")
        if url.startswith("https://"):
            QDesktopServices.openUrl(QUrl(url))

    def prepare_skill(self, repository, directory, ref, scope):
        def completed(plan):
            plan["scope"] = scope
            row = {"id": "pending:" + plan["repository"] + "/" + plan["path"], "skill_name": plan["name"],
                   "kind": "skill", "title": plan["name"], "management": "managed",
                   "source": plan["repository"], "website": "https://github.com/" + plan["repository"],
                   "description": plan["path"], "status": "待安装", "current_version": ""}
            self._plans[self._key(row)] = plan
            self.set_extension(row)
            self.extension_prepared.emit(row)
            self.status.setText(QCoreApplication.translate('ExtensionDetails', '已固定提交，请核对来源后点击安装。'))
        self.run_operation(lambda cancel: self._service.preview_skill(repository, directory, ref, cancelled=cancel),
                           completed, QCoreApplication.translate('ExtensionDetails', '正在检查技能来源…'))
