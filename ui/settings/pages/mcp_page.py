from __future__ import annotations

from services.storage_service import StorageService
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel, QGroupBox

from ui.dialogs.mcp_server_dialog import McpSettingsWidget
from ui.settings.page_header import build_page_header


class McpPage(QWidget):
    page_title = "MCP"

    def __init__(self, storage_service: StorageService | None = None, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(build_page_header("MCP", "管理外部 MCP Server；内置工具由模式和权限页统一控制。"))
        builtin_group = QGroupBox("工具体系说明")
        builtin_layout = QVBoxLayout(builtin_group)
        builtin_layout.setContentsMargins(10, 10, 10, 10)
        builtin_layout.setSpacing(6)
        for text in [
            "内置工具：由 ToolManager 注册，不在本页增删。",
            "模型可见性：由模式的 allowed_tool_categories 和每次请求的 tool_selection 决定。",
            "启用与自动批准：由权限页的 tool_permissions 决定。",
            "外部 MCP：仅在本页配置 Server，工具命名为 mcp__{server}__{tool}。",
        ]:
            line = QLabel(text)
            line.setWordWrap(True)
            line.setProperty("muted", True)
            builtin_layout.addWidget(line)
        layout.addWidget(builtin_group)
        layout.addWidget(McpSettingsWidget(storage_service=storage_service))
        hint = QLabel(
            "MCP 页只负责外部工具来源。若模型看不到 MCP 工具，请同时检查当前模式是否允许 mcp 类别、"
            "权限页是否启用对应工具，以及 MCP Server 是否可用。"
        )
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch()
