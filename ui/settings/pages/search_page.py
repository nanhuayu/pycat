from __future__ import annotations

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QGroupBox, QVBoxLayout as QVBox, QCheckBox

from models.search_config import SearchConfig
from ui.settings.page_header import build_page_header
from ui.utils.form_builder import FormSection


class SearchPage(QWidget):
    page_title = "搜索"

    def __init__(self, search_config: SearchConfig, parent=None):
        super().__init__(parent)
        self._setup_ui(search_config)

    def _setup_ui(self, search_config: SearchConfig) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(build_page_header("搜索", "配置联网搜索服务、凭据和结果数量控制。"))

        enable_group = QGroupBox("搜索服务")
        enable_layout = QVBox(enable_group)
        self.search_enabled_check = QCheckBox("启用网络搜索")
        self.search_enabled_check.setChecked(bool(search_config.enabled))
        self.search_enabled_check.setToolTip("允许模型在需要时搜索互联网获取最新信息")
        enable_layout.addWidget(self.search_enabled_check)
        layout.addWidget(enable_group)

        provider = FormSection("搜索引擎")
        self.search_provider_combo = provider.add_combo(
            "搜索引擎:", items=["Tavily AI", "Google (SerpAPI)", "SearXNG (自托管)"],
            current_index={"tavily": 0, "google": 1, "searxng": 2}.get(search_config.provider, 0),
        )
        self.search_api_key_edit = provider.add_line_edit(
            "API Key:", text=search_config.api_key,
            placeholder="API Key (Tavily/SerpAPI)", echo_password=True,
        )
        self.search_api_base_edit = provider.add_line_edit(
            "API 地址:", text=search_config.api_base,
            placeholder="http://localhost:8888 (仅 SearXNG)",
        )
        layout.addWidget(provider.group)

        options = FormSection("搜索选项")
        results_items = ["3", "5", "10", "20"]
        cur = str(getattr(search_config, "max_results", 5))
        idx = results_items.index(cur) if cur in results_items else 1
        self.search_max_results = options.add_combo(
            "结果数量:", items=results_items, current_index=idx,
        )
        self.search_include_date = options.add_checkbox(
            "结果包含日期", checked=bool(search_config.include_date),
        )
        layout.addWidget(options.group)
        layout.addStretch()

    def collect(self) -> SearchConfig:
        providers_map = {0: "tavily", 1: "google", 2: "searxng"}
        return SearchConfig(
            enabled=self.search_enabled_check.isChecked(),
            provider=providers_map.get(self.search_provider_combo.currentIndex(), "tavily"),
            api_key=self.search_api_key_edit.text().strip(),
            api_base=self.search_api_base_edit.text().strip(),
            max_results=int(self.search_max_results.currentText()),
            include_date=self.search_include_date.isChecked(),
        )
