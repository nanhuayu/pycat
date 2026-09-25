from __future__ import annotations

import asyncio

from PyQt6.QtCore import QCoreApplication, QThreadPool
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pycat.core.app.services.search import SearchService
from pycat.gui.runtime.background_job import BackgroundJob
from pycat.gui.settings.page_header import build_page_header
from pycat.gui.utils.form_builder import FormSection
from pycat.gui.utils.icon_manager import Icons
from pycat.models.search_config import MAX_MAX_RESULTS, MIN_MAX_RESULTS, SearchConfig


class SearchPage(QWidget):
    page_title = "搜索"

    _PROVIDERS_META = [
        (item['id'], item['name'], item['requires_api_key'], item['requires_api_base'])
        for item in SearchService.list_providers()
    ]

    def __init__(self, search_config: SearchConfig, parent=None):
        super().__init__(parent)
        self._check_job: BackgroundJob | None = None
        self._check_button_text = QCoreApplication.translate('SearchPage', '检查')
        self._setup_ui(search_config)

    def _setup_ui(self, search_config: SearchConfig) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(build_page_header(QCoreApplication.translate('SearchPage', '搜索'), QCoreApplication.translate('SearchPage', '配置联网搜索服务和结果返回方式。')))

        service = FormSection(QCoreApplication.translate('SearchPage', '搜索服务'))
        self.search_enabled_check = service.add_checkbox(
            QCoreApplication.translate('SearchPage', '启用网络搜索'),
            checked=bool(search_config.enabled),
            tooltip=QCoreApplication.translate('SearchPage', '允许模型在需要时搜索互联网获取最新信息'),
        )

        provider_names = [p[1] for p in self._PROVIDERS_META]
        current_provider = search_config.provider
        current_index = 0
        for i, (pid, _, _, _) in enumerate(self._PROVIDERS_META):
            if pid == current_provider:
                current_index = i
                break

        self.search_provider_combo = service.add_combo(
            QCoreApplication.translate('SearchPage', '搜索引擎'),
            items=provider_names,
            current_index=current_index,
        )
        self.search_provider_combo.currentIndexChanged.connect(self._on_provider_changed)

        self._api_key_label = QLabel('API Key')
        self._api_base_label = QLabel(QCoreApplication.translate('SearchPage', 'API 地址'))
        self.search_api_key_edit = service.add_line_edit(
            self._api_key_label,
            text=search_config.api_key,
            placeholder=QCoreApplication.translate('SearchPage', '输入 API Key'),
            echo_password=True,
        )
        self.search_api_base_edit = service.add_line_edit(
            self._api_base_label,
            text=search_config.api_base,
            placeholder=QCoreApplication.translate('SearchPage', '例如: https://searx.example.com'),
        )

        check_row = QWidget()
        check_layout = QHBoxLayout(check_row)
        check_layout.setContentsMargins(0, 0, 0, 0)
        self._provider_hint = QLabel('')
        self._provider_hint.setObjectName('settings_hint')
        self._provider_hint.setWordWrap(True)
        check_layout.addWidget(self._provider_hint, 1)
        self._check_btn = QPushButton(QCoreApplication.translate('SearchPage', '检查'))
        self._check_btn.setObjectName("settings_action_btn")
        self._check_btn.setIcon(Icons.get(Icons.CHECK))
        self._check_btn.clicked.connect(self._on_check_clicked)
        check_layout.addWidget(self._check_btn)
        service.form.addRow(check_row, info=True)
        layout.addWidget(service.group)

        options = FormSection(QCoreApplication.translate('SearchPage', '结果'))
        self.search_max_results = options.add_spin(
            QCoreApplication.translate('SearchPage', '结果数量'),
            value=search_config.max_results,
            range=(MIN_MAX_RESULTS, MAX_MAX_RESULTS),
        )
        self.search_include_date = options.add_checkbox(
            QCoreApplication.translate('SearchPage', '包含日期'),
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
        self._api_key_label.setVisible(needs_key)
        self._api_base_label.setVisible(needs_base)
        self._check_btn.setVisible(True)

        hints = {
            "duckduckgo": QCoreApplication.translate('SearchPage', 'DuckDuckGo 无需配置。'),
            "google": QCoreApplication.translate('SearchPage', 'Google 无需密钥；受网络连接和搜索站点限流影响。'),
            "bing": QCoreApplication.translate('SearchPage', 'Bing 使用公开 RSS，无需密钥；受站点可用性影响。'),
            "tavily": QCoreApplication.translate('SearchPage', 'Tavily 需要 API Key，适合需要摘要质量的搜索。'),
            "brave": QCoreApplication.translate('SearchPage', 'Brave Search 需要 API Key，适合隐私友好的通用搜索。'),
            "searxng": QCoreApplication.translate('SearchPage', 'SearXNG 需要填写自托管实例地址。'),
        }
        self._provider_hint.setText(hints.get(pid, ""))

    def _on_check_clicked(self):
        if self._check_job is not None:
            return
        config = self.collect()
        service = SearchService(config)
        self._check_btn.setEnabled(False)
        old_text = self._check_btn.text()
        self._check_button_text = old_text
        self._check_btn.setText(QCoreApplication.translate('SearchPage', '检查中'))

        job = BackgroundJob(lambda: asyncio.run(service.check()))
        self._check_job = job
        job.signals.finished.connect(self._finish_check)
        QThreadPool.globalInstance().start(job)

    def _finish_check(self, result, error) -> None:
        job = self._check_job
        if job is None:
            return
        self._check_job = None
        self._check_btn.setText(self._check_button_text)
        self._check_btn.setEnabled(True)
        if error is not None:
            valid, message = False, str(error)
        else:
            valid, message = result

        if valid:
            QMessageBox.information(self, QCoreApplication.translate('SearchPage', '连接测试'), QCoreApplication.translate('SearchPage', '搜索配置可用。'))
        else:
            msg = QCoreApplication.translate('SearchPage', '连接失败: {message}').format(message=message) if message else QCoreApplication.translate('SearchPage', '连接失败')
            QMessageBox.warning(self, QCoreApplication.translate('SearchPage', '连接测试'), msg)

    def collect(self) -> SearchConfig:
        index = self.search_provider_combo.currentIndex()
        provider_id = self._PROVIDERS_META[index][0] if 0 <= index < len(self._PROVIDERS_META) else "duckduckgo"
        return SearchConfig(
            enabled=self.search_enabled_check.isChecked(),
            provider=provider_id,
            api_key=self.search_api_key_edit.text().strip(),
            api_base=self.search_api_base_edit.text().strip(),
            max_results=self.search_max_results.value(),
            include_date=self.search_include_date.isChecked(),
        )
