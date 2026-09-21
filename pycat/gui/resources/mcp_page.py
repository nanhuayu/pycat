from __future__ import annotations

from collections.abc import Callable, Iterable

from PyQt6.QtWidgets import QWidget, QVBoxLayout

from pycat.gui.resources.mcp_editor import McpSettingsWidget
from pycat.gui.settings.page_header import build_page_header
from pycat.models.contracts.mcp import McpServerConfig


class McpPage(QWidget):
    page_title = "MCP"

    def __init__(
        self,
        servers: Iterable[McpServerConfig] = (),
        *,
        reload_provider: Callable[[], Iterable[McpServerConfig]] | None = None,
        connection_tester: Callable[[McpServerConfig], dict] | None = None,
        parent=None,
        show_header=True,
    ):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        if show_header:
            layout.addWidget(build_page_header("MCP", "加载和管理外部 MCP Server。工具权限由“权限”页统一控制。"))
        self.editor = McpSettingsWidget(
            servers=servers,
            reload_provider=reload_provider,
            connection_tester=connection_tester,
        )
        layout.addWidget(self.editor, 1)

    def collect_servers(self):
        return self.editor.collect_servers()

    def mark_saved(self) -> None:
        self.editor.mark_saved()
