from __future__ import annotations

from services.storage_service import StorageService
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel

from ui.dialogs.mcp_server_dialog import McpSettingsWidget
from ui.settings.page_header import build_page_header


class McpPage(QWidget):
    page_title = "MCP"

    def __init__(self, storage_service: StorageService | None = None, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(build_page_header("MCP", "加载和管理外部 MCP Server。工具权限由“权限”页统一控制。"))
        layout.addWidget(McpSettingsWidget(storage_service=storage_service))
        hint = QLabel("若模型看不到 MCP 工具，请检查当前模式是否允许 mcp 类别、权限页是否启用对应工具。")
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch()
