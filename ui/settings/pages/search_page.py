from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QMessageBox,
)

from models.search_config import SearchConfig
from services.search_service import SearchService
from ui.settings.page_header import build_page_header
from ui.utils.icon_manager import Icons
from ui.utils.form_builder import FormSection


class SearchPage(QWidget):
    page_title = "搜索"

    _PROVIDERS_META = [
        ("duckduckgo", "DuckDuckGo", False, False),
        ("tavily", "Tavily AI", True, False),
        ("brave", "Brave Search", True, False),
        ("searxng", "SearXNG (自托管)", False, True),
    ]

    def __init__(self, search_config: SearchConfig, parent=None):
        super().__init__(parent)
        self._setup_ui(search_config)

    def _setup_ui(self, search_config: SearchConfig) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(build_page_header("搜索", "配置联网搜索服务和结果返回方式。"))

        service = FormSection("搜索服务")
        self.search_enabled_check = service.add_checkbox(
            "启用网络搜索",
            checked=bool(search_config.enabled),
            tooltip="允许模型在需要时搜索互联网获取最新信息",
        )

        provider_names = [p[1] for p in self._PROVIDERS_META]
        current_provider = search_config.provider
        current_index = 0
        for i, (pid, _, _, _) in enumerate(self._PROVIDERS_META):
            if pid == current_provider:
                current_index = i
                break

        self.search_provider_combo = service.add_combo(
            "搜索引擎",
            items=provider_names,
            current_index=current_index,
        )
        self.search_provider_combo.currentIndexChanged.connect(self._on_provider_changed)

        self.search_api_key_edit = service.add_line_edit(
            "API Key",
            text=search_config.api_key,
            placeholder="输入 API Key",
            echo_password=True,
        )
        self.search_api_base_edit = service.add_line_edit(
            "API 地址",
            text=search_config.api_base,
            placeholder="例如: https://searx.example.com",
        )

        self._api_key_label = service.form.labelForField(self.search_api_key_edit)
        self._api_base_label = service.form.labelForField(self.search_api_base_edit)

        check_row = QWidget()
        check_layout = QHBoxLayout(check_row)
        check_layout.setContentsMargins(0, 0, 0, 0)
        check_layout.addStretch()
        self._check_btn = QPushButton("检查")
        self._check_btn.setObjectName("settings_action_btn")
        self._check_btn.setIcon(Icons.get(Icons.CHECK))
        self._check_btn.clicked.connect(self._on_check_clicked)
        check_layout.addWidget(self._check_btn)
        service.form.addRow("", check_row)

        self._provider_hint = QLabel("")
        self._provider_hint.setObjectName("settings_hint")
        self._provider_hint.setWordWrap(True)
        service.form.addRow(self._provider_hint)
        layout.addWidget(service.group)

        options = FormSection("结果")
        results_items = ["3", "5", "10", "20"]
        cur = str(getattr(search_config, "max_results", 5))
        idx = results_items.index(cur) if cur in results_items else 1
        self.search_max_results = options.add_combo(
            "结果数量",
            items=results_items,
            current_index=idx,
        )
        self.search_include_date = options.add_checkbox(
            "包含日期",
            checked=bool(search_config.include_date),
        )
        layout.addWidget(options.group)
        layout.addStretch()

        self._on_provider_changed(current_index)

    def _on_provider_changed(self, index: int):
        if index < 0 or index >= len(self._PROVIDERS_META):
            return
        pid, _name, needs_key, needs_base = self._PROVIDERS_META[index]

        self.search_api_key_edit.setVisible(needs_key)
        self.search_api_base_edit.setVisible(needs_base)
        if self._api_key_label is not None:
            self._api_key_label.setVisible(needs_key)
        if self._api_base_label is not None:
            self._api_base_label.setVisible(needs_base)
        self._check_btn.setVisible(needs_key or needs_base)

        hints = {
            "duckduckgo": "DuckDuckGo 无需配置。",
            "tavily": "Tavily 需要 API Key，适合需要摘要质量的搜索。",
            "brave": "Brave Search 需要 API Key，适合隐私友好的通用搜索。",
            "searxng": "SearXNG 需要填写自托管实例地址。",
        }
        self._provider_hint.setText(hints.get(pid, ""))

    def _on_check_clicked(self):
        config = self.collect()
        service = SearchService(config)
        self._check_btn.setEnabled(False)
        old_text = self._check_btn.text()
        self._check_btn.setText("检查中")
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                valid, error = executor.submit(lambda: asyncio.run(service.check())).result()
        except Exception as e:
            valid, error = False, str(e)
        finally:
            self._check_btn.setText(old_text)
            self._check_btn.setEnabled(True)

        if valid:
            QMessageBox.information(self, "连接测试", "搜索配置可用。")
        else:
            msg = f"连接失败: {error}" if error else "连接失败"
            QMessageBox.warning(self, "连接测试", msg)

    def collect(self) -> SearchConfig:
        index = self.search_provider_combo.currentIndex()
        provider_id = self._PROVIDERS_META[index][0] if 0 <= index < len(self._PROVIDERS_META) else "duckduckgo"
        return SearchConfig(
            enabled=self.search_enabled_check.isChecked(),
            provider=provider_id,
            api_key=self.search_api_key_edit.text().strip(),
            api_base=self.search_api_base_edit.text().strip(),
            max_results=int(self.search_max_results.currentText()),
            include_date=self.search_include_date.isChecked(),
        )
