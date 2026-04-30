"""Settings dialog (thin container).

This dialog hosts modular setting pages under `ui.settings.pages`.

Notes:
- Modes are user-wide (APPDATA/PyCat/modes.json), edited via `ModesPage`.
- Prompt templates (default/system guidelines, optimizer templates) remain user-wide in settings.json.
"""

from __future__ import annotations

import logging
from typing import List

from PyQt6.QtCore import Qt, pyqtSignal, QSize
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QVBoxLayout,
    QListWidget,
    QListWidgetItem,
    QStackedWidget,
    QPushButton,
    QFrame,
    QSizePolicy,
    QMessageBox,
    QScrollArea,
)

from core.app import AppSettingsUpdate
from core.channel.runtime import ChannelRuntimeService
from models.provider import Provider
from services.provider_catalog_service import ProviderCatalogService
from services.provider_service import ProviderService
from services.storage_service import StorageService
from core.config.schema import AppConfig

from ui.settings.pages import (
    ModelsPage,
    AgentPermissionsPage,
    ChannelsPage,
    TerminalPage,
    McpPage,
    SkillsPage,
    AppearancePage,
    ContextPage,
    PromptsPage,
    ModesPage,
    SearchPage,
    AboutPage,
)
from ui.utils.icon_manager import Icons


logger = logging.getLogger(__name__)


# 页面 emoji 到统一图标的映射
_PAGE_ICON_MAP = {
    "ModelsPage": Icons.PAGE_MODELS,
    "AgentPermissionsPage": Icons.PAGE_AGENTS,
    "AppearancePage": Icons.PAGE_APPEARANCE,
    "ChannelsPage": Icons.PAGE_CHANNELS,
    "TerminalPage": Icons.PAGE_TERMINAL_SETTINGS,
    "McpPage": Icons.PAGE_MCP,
    "SkillsPage": Icons.PAGE_SKILLS,
    "ContextPage": Icons.PAGE_CONTEXT,
    "PromptsPage": Icons.PAGE_PROMPTS,
    "ModesPage": Icons.PAGE_MODES,
    "SearchPage": Icons.PAGE_SEARCH,
    "AboutPage": Icons.PAGE_ABOUT,
}


def _get_page_icon(page) -> QIcon:
    """根据页面类型获取统一图标。"""
    page_class = page.__class__.__name__
    icon_name = _PAGE_ICON_MAP.get(page_class)
    if icon_name:
        return Icons.get(icon_name, scale_factor=1.0)
    return Icons.get(Icons.SETTINGS, scale_factor=1.0)


class SettingsDialog(QDialog):
    """Thin container dialog."""

    providers_changed = pyqtSignal()

    def __init__(
        self,
        providers: List[Provider],
        current_settings: dict | None = None,
        provider_service: ProviderService | None = None,
        provider_catalog_service: ProviderCatalogService | None = None,
        storage_service: StorageService | None = None,
        channel_runtime: ChannelRuntimeService | None = None,
        parent=None,
        work_dir: str | None = None,
    ):
        super().__init__(parent)
        self.providers = list(providers or [])
        self.current_settings = current_settings or {}
        self.work_dir = str(work_dir or "")

        self.provider_service = provider_service or ProviderService()
        self.storage = storage_service or StorageService()
        self.provider_catalog_service = provider_catalog_service or ProviderCatalogService(
            storage=self.storage,
            provider_service=self.provider_service,
        )
        self.channel_runtime = channel_runtime
        self.providers = self.provider_catalog_service.snapshot(self.providers)
        self.search_config = self.storage.load_search_config()
        self._app_config = AppConfig.from_dict(self.current_settings)

        self._appearance_patch: dict = {}
        self._general_patch: dict = {}
        self._models_patch: dict = {}
        self._auto_approve_patch: dict = {}
        self._context_patch: dict = {}
        self._prompt_patch: dict = {}
        self._channels_patch: dict = {}
        self._terminal_patch: dict = {}

        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setWindowTitle("设置")
        self.setObjectName("settings_dialog")
        self.setModal(True)
        self.setMinimumSize(800, 560)
        try:
            self.resize(850, 600)
        except Exception as exc:
            logger.debug("Failed to resize settings dialog: %s", exc)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("settings_sidebar")
        sidebar.setFixedWidth(224)

        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(12, 12, 12, 12)
        sidebar_layout.setSpacing(6)

        self.page_list = QListWidget()
        self.page_list.setObjectName("settings_nav")
        self.page_list.setIconSize(QSize(20, 20))
        self.page_list.setSpacing(2)
        self.page_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.page_list.currentRowChanged.connect(self._change_page)
        sidebar_layout.addWidget(self.page_list, 1)

        sidebar_layout.addSpacing(6)
        save_btn = QPushButton("保存")
        save_btn.setProperty("primary", True)
        save_btn.clicked.connect(self.accept)
        sidebar_layout.addWidget(save_btn)

        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        sidebar_layout.addWidget(cancel_btn)

        layout.addWidget(sidebar)

        self.content = QStackedWidget()
        self.content.setObjectName("settings_content")
        layout.addWidget(self.content)

        self._init_pages()
        self.page_list.setCurrentRow(0)

    def _init_pages(self) -> None:
        self.page_list.clear()

        self.models_page = ModelsPage(
            self.providers,
            default_chat_model=str(getattr(self._app_config, "default_chat_model", "") or ""),
            provider_service=self.provider_service,
            provider_catalog_service=self.provider_catalog_service,
        )
        self.agent_page = AgentPermissionsPage(self._app_config.permissions, retry=self._app_config.retry)
        self.channels_page = ChannelsPage(self._app_config.channels, channel_runtime=self.channel_runtime)
        self.terminal_page = TerminalPage(self._app_config.shell)
        self.mcp_page = McpPage(storage_service=self.storage)
        self.skills_page = SkillsPage(work_dir=self.work_dir)
        self.appearance_page = AppearancePage(
            theme=self._app_config.theme,
            show_stats=self._app_config.show_stats,
            show_thinking=self._app_config.show_thinking,
            log_stream=self._app_config.log_stream,
            proxy_url=self._app_config.proxy_url,
            llm_timeout_seconds=float(getattr(self._app_config, "llm_timeout_seconds", 600.0) or 600.0),
        )
        self.context_page = ContextPage(self._app_config.context, providers=self.providers)
        self.prompts_page = PromptsPage(
            self._app_config.prompts,
            self._app_config.prompt_optimizer,
            providers=self.providers,
            prompt_optimizer_model=str(getattr(self._app_config, "prompt_optimizer_model", "") or ""),
        )
        # Modes are global user config; ModesPage ignores work_dir.
        self.modes_page = ModesPage(self.work_dir)
        self.search_page = SearchPage(self.search_config)
        self.about_page = AboutPage()

        self._pages = [
            self.models_page,
            self.agent_page,
            self.appearance_page,
            self.channels_page,
            self.terminal_page,
            self.mcp_page,
            self.skills_page,
            self.context_page,
            self.prompts_page,
            self.modes_page,
            self.search_page,
            self.about_page,
        ]

        for page in self._pages:
            self.content.addWidget(self._wrap_page(page))
            title = str(getattr(page, "page_title", "设置"))
            icon = _get_page_icon(page)
            item = QListWidgetItem(icon, title)
            item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self.page_list.addItem(item)

        try:
            self.models_page.providers_changed.connect(self.providers_changed)
        except Exception as exc:
            logger.debug("Failed to connect providers_changed signal in settings dialog: %s", exc)

    def _wrap_page(self, page) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setObjectName("settings_page_scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(page)
        return scroll

    def _change_page(self, index: int) -> None:
        self.content.setCurrentIndex(int(index))

    def accept(self) -> None:
        try:
            if not self.modes_page.save_to_disk():
                QMessageBox.warning(self, "模式配置无效", "请先修正 modes.json 后再保存。")
                return
        except Exception as exc:
            logger.debug("Failed to persist modes configuration from settings dialog: %s", exc)

        try:
            self.providers = self.models_page.get_providers()
        except Exception as exc:
            logger.debug("Failed to collect providers from settings dialog: %s", exc)

        try:
            self._models_patch = {
                "default_chat_model": self.models_page.collect_default_chat_model(),
            }
        except Exception as exc:
            logger.debug("Failed to collect default chat model from settings dialog: %s", exc)
            self._models_patch = {}

        try:
            self.search_config = self.search_page.collect()
            self.storage.save_search_config(self.search_config)
        except Exception as exc:
            logger.debug("Failed to collect or save search configuration: %s", exc)

        try:
            self._appearance_patch = dict(self.appearance_page.collect() or {})
            self._general_patch = dict(self._appearance_patch)
        except Exception:
            self._appearance_patch = {}
            self._general_patch = {}

        try:
            perms = self.agent_page.collect()
            self._auto_approve_patch = perms.to_dict()
        except Exception as exc:
            logger.debug("Failed to collect agent permission settings: %s", exc)
            self._auto_approve_patch = {}

        try:
            self._retry_patch = self.agent_page.collect_retry().to_dict()
        except Exception:
            self._retry_patch = {}

        try:
            ctx = self.context_page.collect()
            self._context_patch = {"context": ctx.to_dict()}
        except Exception:
            self._context_patch = {}

        try:
            prompts = self.prompts_page.collect_prompts()
            opt = self.prompts_page.collect_prompt_optimizer()
            self._prompt_patch = {
                "prompts": prompts.to_dict(),
                "prompt_optimizer": opt.to_dict(),
                "prompt_optimizer_model": self.prompts_page.collect_prompt_optimizer_model(),
            }
        except Exception:
            self._prompt_patch = {}

        try:
            self._channels_patch = {
                "channels": [channel.to_dict() for channel in self.channels_page.collect()],
            }
        except Exception:
            self._channels_patch = {}

        try:
            self._terminal_patch = dict(self.terminal_page.collect() or {})
        except Exception:
            self._terminal_patch = {}

        super().accept()

    def get_providers(self) -> List[Provider]:
        return list(self.providers or [])

    def build_update(self) -> AppSettingsUpdate:
        settings_patch = {
            "show_stats": self.get_show_stats(),
            "theme": self.get_theme(),
            "show_thinking": self.get_show_thinking(),
            "log_stream": self.get_log_stream(),
            "proxy_url": self.get_proxy_url(),
            "llm_timeout_seconds": self.get_llm_timeout_seconds(),
        }
        settings_patch.update(self.get_auto_approve_settings())
        settings_patch.update(self.get_model_settings())

        retry_patch = self.get_retry_settings()
        if retry_patch:
            settings_patch["retry"] = retry_patch

        settings_patch.update(self.get_context_settings())
        settings_patch.update(self.get_prompt_settings())
        settings_patch.update(self.get_channel_settings())
        settings_patch.update(self.get_terminal_settings())

        return AppSettingsUpdate(
            providers=tuple(self.get_providers()),
            settings_patch=settings_patch,
        )

    def get_theme(self) -> str:
        return str(self._appearance_patch.get("theme") or self._app_config.theme)

    def get_show_stats(self) -> bool:
        return bool(self._appearance_patch.get("show_stats", self._app_config.show_stats))

    def get_show_thinking(self) -> bool:
        return bool(self._appearance_patch.get("show_thinking", self._app_config.show_thinking))

    def get_log_stream(self) -> bool:
        return bool(self._appearance_patch.get("log_stream", self._app_config.log_stream))

    def get_proxy_url(self) -> str:
        return str(self._general_patch.get("proxy_url", self._app_config.proxy_url) or "")

    def get_llm_timeout_seconds(self) -> float:
        try:
            return float(self._general_patch.get("llm_timeout_seconds", self._app_config.llm_timeout_seconds))
        except Exception:
            return float(getattr(self._app_config, "llm_timeout_seconds", 600.0) or 600.0)

    def get_auto_approve_settings(self) -> dict:
        return dict(self._auto_approve_patch or {})

    def get_model_settings(self) -> dict:
        return dict(self._models_patch or {})

    def get_retry_settings(self) -> dict:
        return dict(self._retry_patch if hasattr(self, '_retry_patch') else {})

    def get_context_settings(self) -> dict:
        return dict(self._context_patch or {})

    def get_prompt_settings(self) -> dict:
        return dict(self._prompt_patch or {})

    def get_channel_settings(self) -> dict:
        return dict(self._channels_patch or {})

    def get_preferred_channel_session_id(self) -> str:
        try:
            return str(self.channels_page.get_preferred_session_id() or "").strip()
        except Exception:
            return ""

    def get_terminal_settings(self) -> dict:
        return dict(self._terminal_patch or {})
