"""One resource type under Settings: installed items and market discovery."""
from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QLabel, QPushButton, QStackedWidget, QTabBar, QVBoxLayout, QWidget

from pycat.gui.resources.discovery import DiscoveryPanel
from pycat.gui.resources.mcp_page import McpPage
from pycat.gui.resources.skills_page import SkillsPage


class ResourcePage(QWidget):
    changed = pyqtSignal()

    def __init__(self, *, kind, mcp_servers=(), skill_service=None, extension_service=None,
                 work_dir="", reload_provider=None, connection_tester=None,
                 servers_provider=None, candidate_evaluator=None, parent=None):
        super().__init__(parent)
        self.kind = kind
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)
        self.view_tabs = QTabBar()
        for title in ("已安装", "市场"):
            self.view_tabs.addTab(title)
        self.view_tabs.setExpanding(False)
        root.addWidget(self.view_tabs)
        self.content = QStackedWidget()
        if kind == "skill":
            self.installed = SkillsPage(work_dir=work_dir, skill_service=skill_service,
                candidate_evaluator=candidate_evaluator, show_header=False)
        else:
            self.installed = McpPage(servers=mcp_servers, reload_provider=reload_provider,
                connection_tester=connection_tester, show_header=False)
        self.market = DiscoveryPanel(kind=kind, service=extension_service, work_dir=work_dir,
            servers_provider=servers_provider or (self.installed.collect_servers if kind == "mcp" else lambda: ()), show_header=False)
        if kind == "skill":
            self.market.skills_changed.connect(self.installed._refresh_list)
            browser = self.installed.list_body
        else:
            self.market.mcp_prepared.connect(self.stage_mcp)
            browser = self.installed.editor.browser
        for page in (self.installed, self.market):
            page.layout().setContentsMargins(0, 0, 0, 0)
            self.content.addWidget(page)
        root.addWidget(self.content, 1)
        self.view_tabs.currentChanged.connect(self.content.setCurrentIndex)
        self.update_btn = QPushButton("版本与更新")
        self.update_btn.clicked.connect(self._show_update)
        browser.header_layout.addWidget(self.update_btn)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        root.addWidget(self.status_label)

    def show_market(self, extension_id=""):
        self.view_tabs.setCurrentIndex(1)
        if extension_id:
            self.market.show_installed(extension_id)

    def _show_update(self):
        try:
            if self.kind == "skill":
                skill = self.installed._current_skill()
                if skill:
                    self.show_market(skill.name)
            else:
                servers = self.installed.collect_servers()
                index = self.installed.editor._active_index
                if 0 <= index < len(servers):
                    server = servers[index]
                    self.show_market("agent-browser" if server.integration == "agent-browser" else "configured:" + server.name)
        except Exception as exc:
            self.status_label.setText(str(exc))
            self.status_label.show()

    def stage_mcp(self, config):
        editor = self.installed.editor
        servers = editor.collect_servers()
        index = next((i for i, item in enumerate(servers) if item.name == config.name), len(servers))
        if index == len(servers):
            servers.append(config)
        else:
            servers[index] = config
        editor.servers = servers
        editor.refresh_list(preferred_index=index)
        self.changed.emit()

    def cancel_pending(self):
        self.market.cancel_pending()
        if self.kind == "mcp":
            self.installed.editor.cancel_probe()
