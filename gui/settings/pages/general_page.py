"""General settings grouped into focused tabs."""

from __future__ import annotations

from PyQt6.QtWidgets import QTabWidget, QVBoxLayout, QWidget

from gui.settings.page_header import build_page_header
from gui.settings.pages.appearance_page import AppearancePage
from gui.settings.pages.instructions_page import InstructionsPage
from gui.settings.pages.search_page import SearchPage
from gui.settings.pages.terminal_page import TerminalPage
from models.contracts.config import PromptsConfig, ShellConfig
from models.search_config import SearchConfig


class GeneralPage(QWidget):
    page_title = "通用"

    def __init__(
        self,
        *,
        theme: str,
        accent: str,
        show_thinking: bool,
        close_to_tray: bool,
        log_stream: bool,
        proxy_url: str,
        llm_timeout_seconds: float,
        shell_config: ShellConfig,
        search_config: SearchConfig,
        prompts: PromptsConfig | None = None,
        parent=None,
    ):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(build_page_header("通用", "集中管理界面、全局指令、基础工具和联网搜索。"))

        self.tabs = QTabWidget()
        self.tabs.setObjectName("settings_subtabs")
        self.appearance_page = AppearancePage(
            theme=theme,
            accent=accent,
            show_thinking=show_thinking,
            close_to_tray=close_to_tray,
            log_stream=log_stream,
            proxy_url=proxy_url,
            llm_timeout_seconds=llm_timeout_seconds,
            embedded=True,
        )
        self.instructions_page = InstructionsPage(prompts or PromptsConfig(), embedded=True)
        self.terminal_page = TerminalPage(shell_config, embedded=True)
        self.search_page = SearchPage(search_config, embedded=True)
        self.tabs.addTab(self.appearance_page, "界面")
        self.tabs.addTab(self.instructions_page, "指令")
        self.tabs.addTab(self.terminal_page, "终端")
        self.tabs.addTab(self.search_page, "搜索")
        layout.addWidget(self.tabs, 1)
