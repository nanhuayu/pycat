from __future__ import annotations

from collections.abc import Callable, Iterable

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel

from gui.settings.mcp_editor import McpSettingsWidget
from gui.settings.page_header import build_page_header
from models.contracts.mcp import McpServerConfig


class McpPage(QWidget):
    page_title = "MCP"

    def __init__(
        self,
        servers: Iterable[McpServerConfig] = (),
        *,
        reload_provider: Callable[[], Iterable[McpServerConfig]] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(build_page_header("MCP", "加载和管理外部 MCP Server。工具权限由“权限”页统一控制。"))
        self.editor = McpSettingsWidget(
            servers=servers,
            reload_provider=reload_provider,
        )
        layout.addWidget(self.editor, 1)

    def collect_servers(self):
        return self.editor.collect_servers()

    def mark_saved(self) -> None:
        self.editor.mark_saved()
