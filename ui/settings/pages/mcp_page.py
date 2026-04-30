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

        layout.addWidget(build_page_header("MCP", "统一管理内置工具与外部 MCP 服务，减少工具入口分散。"))
        builtin_group = QGroupBox("内置工具")
        builtin_layout = QVBoxLayout(builtin_group)
        builtin_layout.setContentsMargins(10, 10, 10, 10)
        builtin_layout.setSpacing(6)
        for text in [
            "builtin_filesystem_ls：列出目录结构，用于快速浏览工作区。",
            "builtin_filesystem_read：读取文本文件内容，便于补充上下文。",
            "builtin_filesystem_grep：按关键字检索文件内容。",
            "builtin_python_exec：执行受控 Python 代码，用于小规模分析与验证。",
        ]:
            line = QLabel(text)
            line.setWordWrap(True)
            line.setProperty("muted", True)
            builtin_layout.addWidget(line)
        layout.addWidget(builtin_group)
        layout.addWidget(McpSettingsWidget(storage_service=storage_service))
        hint = QLabel(
            "启用 MCP 后，应用会自动提供内置工具（builtin_filesystem_ls/read/grep、builtin_python_exec），\n"
            "无需额外安装服务器；也可在上方添加外部 MCP Server 扩展能力。"
        )
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch()
