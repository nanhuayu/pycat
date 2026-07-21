from __future__ import annotations

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel

from gui.settings.mcp_editor import McpSettingsWidget
from gui.settings.page_header import build_page_header
from core.app.repositories.mcp_server import McpServerRepository


class McpPage(QWidget):
    page_title = "MCP"

    def __init__(self, repository: McpServerRepository | None = None, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(build_page_header("MCP", "加载和管理外部 MCP Server。工具权限由“权限”页统一控制。"))
        self.editor = McpSettingsWidget(repository=repository)
        layout.addWidget(self.editor, 1)

    def collect_servers(self):
        return self.editor.collect_servers()
