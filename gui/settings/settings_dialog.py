"""Settings dialog (thin container).

This dialog hosts modular setting pages under `gui.settings.pages`.

Notes:
- Modes are user-wide (APPDATA/PyCat/modes.json), edited via `ModesPage`.
- Capability templates and optimizer compatibility settings remain user-wide in settings.json.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
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
from core.app.services.channel import ChannelService
from core.tools.manager import ToolManager
from models.provider import Provider
from core.app.services.provider_catalog import ProviderCatalogService
from core.app.services.provider import ProviderService
from core.app.services.mode_catalog import ModeCatalogService
from core.app.repositories import AppRepositories
from models.contracts.config import AppConfig

from gui.settings.pages import (
    ModelsPage,
    StrategyPage,
    PermissionsPage,
    ChannelsPage,
    McpPage,
    SkillsPage,
    CapabilitiesPage,
    ModesPage,
    AboutPage,
    GeneralPage,
)
from gui.utils.icon_manager import Icons
from gui.utils.theme import theme_tokens
from gui.utils.window_geometry import apply_workbench_dialog_size
from gui.settings.components import SETTINGS_NAV_WIDTH


logger = logging.getLogger(__name__)


PAGE_INDEX_ROLE = Qt.ItemDataRole.UserRole + 1
PAGE_KEY_ROLE = Qt.ItemDataRole.UserRole + 2
NAV_GROUP_ROLE = Qt.ItemDataRole.UserRole + 3


@dataclass(frozen=True)
class SettingsPageSpec:
    key: str
    title: str
    group: str
    page: object
    scrollable: bool = True


# 页面 emoji 到统一图标的映射
_PAGE_ICON_MAP = {
    "ModelsPage": Icons.PAGE_MODELS,
    "StrategyPage": Icons.SLIDERS,
    "PermissionsPage": Icons.SHIELD,
    "AppearancePage": Icons.PAGE_APPEARANCE,
    "GeneralPage": Icons.PAGE_APPEARANCE,
    "ChannelsPage": Icons.PAGE_CHANNELS,
    "TerminalPage": Icons.PAGE_TERMINAL_SETTINGS,
    "McpPage": Icons.PAGE_MCP,
    "SkillsPage": Icons.PAGE_SKILLS,
    "CapabilitiesPage": Icons.PAGE_CAPABILITIES,
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
        mode_catalog_service: ModeCatalogService | None = None,
        repositories: AppRepositories | None = None,
        channel_service: ChannelService | None = None,
        tool_manager: ToolManager | None = None,
        parent=None,
        work_dir: str | None = None,
        initial_page: str = "",
        selected_provider_id: str = "",
    ):
        super().__init__(parent)
        self.providers = list(providers or [])
        self.current_settings = current_settings or {}
        self.work_dir = str(work_dir or "")
        self._initial_page = str(initial_page or "").strip().lower()
        self._selected_provider_id = str(selected_provider_id or "").strip()

        self.provider_service = provider_service or ProviderService()
        self.mode_catalog_service = mode_catalog_service or ModeCatalogService()
        self.repositories = repositories or AppRepositories.open()
        self.provider_catalog_service = provider_catalog_service or ProviderCatalogService(
            repository=self.repositories.providers,
            provider_service=self.provider_service,
        )
        if channel_service is None:
            raise ValueError("SettingsDialog requires ChannelService")
        self.channel_service = channel_service
        self.tool_manager = tool_manager
        self.providers = self.provider_catalog_service.snapshot(self.providers)
        self.search_config = self.repositories.search_config.load()
        self._app_config = AppConfig.from_dict(self.current_settings)

        self._appearance_patch: dict = {}
        self._general_patch: dict = {}
        self._models_patch: dict = {}
        self._permissions_patch: dict = {}
        self._agent_patch: dict = {}
        self._context_patch: dict = {}
        self._capability_patch: dict = {}
        self._channels_patch: dict = {}
        self._terminal_patch: dict = {}
        self._mcp_servers = tuple()
        self._modes = tuple()
        self._preferred_channel_session_id = ""

        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setWindowTitle("设置")
        self.setObjectName("settings_dialog")
        self.setModal(True)
        self._apply_initial_size()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("settings_sidebar")
        sidebar.setFixedWidth(SETTINGS_NAV_WIDTH)

        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(10, 10, 10, 10)
        sidebar_layout.setSpacing(6)

        self.page_list = QListWidget()
        self.page_list.setObjectName("settings_nav")
        self.page_list.setIconSize(QSize(Icons.SIZE_SETTINGS_NAV, Icons.SIZE_SETTINGS_NAV))
        self.page_list.setSpacing(1)
        self.page_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.page_list.currentItemChanged.connect(self._change_page)
        sidebar_layout.addWidget(self.page_list, 1)

        sidebar_layout.addSpacing(6)
        save_btn = QPushButton("保存")
        save_btn.setObjectName("settings_action_btn")
        save_btn.setProperty("primary", True)
        save_btn.setIcon(Icons.get(Icons.SAVE, color=theme_tokens("light").color("on_primary")))
        save_btn.clicked.connect(self.accept)
        sidebar_layout.addWidget(save_btn)

        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("settings_action_btn")
        cancel_btn.clicked.connect(self.reject)
        sidebar_layout.addWidget(cancel_btn)

        layout.addWidget(sidebar)

        self.content = QStackedWidget()
        self.content.setObjectName("settings_content")
        layout.addWidget(self.content)

        self._init_pages()
        initial = next(
            (
                spec
                for spec in self._page_specs
                if self._initial_page in {spec.key.lower(), spec.title.lower()}
            ),
            self._page_specs[0],
        )
        self._select_page(initial.page)
        if self._selected_provider_id:
            self.models_page.select_provider(self._selected_provider_id)

    def _apply_initial_size(self) -> None:
        apply_workbench_dialog_size(self)

    def _init_pages(self) -> None:
        self.page_list.clear()

        self.models_page = ModelsPage(
            self.providers,
            default_chat_model=str(getattr(self._app_config, "default_chat_model", "") or ""),
            default_auxiliary_model=str(getattr(self._app_config, "default_auxiliary_model", "") or ""),
            provider_service=self.provider_service,
            provider_catalog_service=self.provider_catalog_service,
        )
        self.strategy_page = StrategyPage(
            agent=self._app_config.agent,
            retry=self._app_config.retry,
            context=self._app_config.context,
        )
        self.permissions_page = PermissionsPage(
            self._app_config.permissions,
            capabilities=getattr(self._app_config, "capabilities", None),
            tool_manager=self.tool_manager,
        )
        self.channels_page = ChannelsPage(
            self._app_config.channels,
            channel_service=self.channel_service,
        )
        self.mcp_page = McpPage(repository=self.repositories.mcp_servers)
        self.skills_page = SkillsPage(work_dir=self.work_dir)
        self.general_page = GeneralPage(
            theme=self._app_config.theme,
            accent=self._app_config.accent,
            show_stats=self._app_config.show_stats,
            show_thinking=self._app_config.show_thinking,
            close_to_tray=self._app_config.close_to_tray,
            log_stream=self._app_config.log_stream,
            proxy_url=self._app_config.proxy_url,
            llm_timeout_seconds=float(getattr(self._app_config, "llm_timeout_seconds", 600.0) or 600.0),
            shell_config=self._app_config.shell,
            search_config=self.search_config,
            prompts=self._app_config.prompts,
        )
        self.appearance_page = self.general_page.appearance_page
        self.instructions_page = self.general_page.instructions_page
        self.terminal_page = self.general_page.terminal_page
        self.search_page = self.general_page.search_page
        self.capabilities_page = CapabilitiesPage(
            self._app_config.prompts,
            providers=self.providers,
            capabilities=getattr(self._app_config, "capabilities", None),
        )
        # Modes are global user config; ModesPage ignores work_dir.
        self.modes_page = ModesPage(
            self.work_dir,
            providers=self.providers,
            mode_catalog=self.mode_catalog_service,
        )
        self.about_page = AboutPage()
        self.models_page.providers_changed.connect(self._sync_provider_dependent_pages)
        self.models_page.providers_changed.connect(self.providers_changed)

        self._page_specs = [
            SettingsPageSpec("general", "通用", "基础", self.general_page),
            SettingsPageSpec("models", "模型", "基础", self.models_page, scrollable=False),
            SettingsPageSpec("modes", "模式", "Agent", self.modes_page, scrollable=False),
            SettingsPageSpec("strategy", "策略", "Agent", self.strategy_page),
            SettingsPageSpec("permissions", "权限", "Agent", self.permissions_page, scrollable=False),
            SettingsPageSpec("capabilities", "能力", "Agent", self.capabilities_page, scrollable=False),
            SettingsPageSpec("skills", "技能", "扩展", self.skills_page, scrollable=False),
            SettingsPageSpec("mcp", "MCP", "扩展", self.mcp_page, scrollable=False),
            SettingsPageSpec("channels", "频道", "集成", self.channels_page, scrollable=False),
            SettingsPageSpec("about", "关于", "其他", self.about_page),
        ]
        self._pages = [spec.page for spec in self._page_specs]
        self._nav_rows_by_page: dict[object, int] = {}

        current_group = ""
        for page_index, spec in enumerate(self._page_specs):
            if spec.group != current_group:
                current_group = spec.group
                header = QListWidgetItem(current_group)
                header.setData(NAV_GROUP_ROLE, True)
                header.setFlags(Qt.ItemFlag.NoItemFlags)
                header.setSizeHint(QSize(0, 24))
                font = header.font()
                font.setPointSize(max(8, font.pointSize() - 1))
                font.setBold(True)
                header.setFont(font)
                self.page_list.addItem(header)

            self.content.addWidget(self._wrap_page(spec.page) if spec.scrollable else spec.page)
            item = QListWidgetItem(_get_page_icon(spec.page), spec.title)
            item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            item.setData(PAGE_INDEX_ROLE, page_index)
            item.setData(PAGE_KEY_ROLE, spec.key)
            self.page_list.addItem(item)
            self._nav_rows_by_page[spec.page] = self.page_list.count() - 1

    def _sync_provider_dependent_pages(self) -> None:
        providers = self.provider_catalog_service.snapshot(self.models_page.providers)
        self.capabilities_page.set_providers(providers)
        self.modes_page.set_providers(providers)

    def _wrap_page(self, page) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setObjectName("settings_page_scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(page)
        return scroll

    def _change_page(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current is None:
            return
        index = current.data(PAGE_INDEX_ROLE)
        if index is None:
            return
        self.content.setCurrentIndex(int(index))

    def _select_page(self, page: object) -> None:
        row = self._nav_rows_by_page.get(page, -1)
        if row >= 0:
            self.page_list.setCurrentRow(row)

    def accept(self) -> None:
        try:
            self._modes = tuple(self.modes_page.collect_modes())
        except Exception as exc:
            QMessageBox.warning(self, "模式配置无效", str(exc))
            self._select_page(self.modes_page)
            return

        try:
            self._mcp_servers = tuple(self.mcp_page.collect_servers())
        except Exception as exc:
            QMessageBox.warning(self, "MCP 配置无效", str(exc))
            self._select_page(self.mcp_page)
            return

        try:
            self.providers = self.models_page.get_providers()
        except Exception as exc:
            QMessageBox.warning(self, "服务商配置无效", str(exc))
            self._select_page(self.models_page)
            return

        try:
            self._models_patch = {
                "default_chat_model": self.models_page.collect_default_chat_model(),
                "default_auxiliary_model": self.models_page.collect_default_auxiliary_model(),
            }
        except Exception as exc:
            QMessageBox.warning(self, "默认模型无效", str(exc))
            self._select_page(self.models_page)
            return

        try:
            self.search_config = self.search_page.collect()
        except Exception as exc:
            QMessageBox.warning(self, "搜索配置无效", str(exc))
            self._select_page(self.general_page)
            return

        try:
            self._appearance_patch = dict(self.appearance_page.collect() or {})
            self._general_patch = dict(self._appearance_patch)
        except Exception as exc:
            QMessageBox.warning(self, "通用配置无效", str(exc))
            self._select_page(self.general_page)
            return

        try:
            perms = self.permissions_page.collect()
            self._permissions_patch = {"permissions": perms.to_dict()}
        except Exception as exc:
            QMessageBox.warning(self, "权限配置无效", str(exc))
            self._select_page(self.permissions_page)
            return

        try:
            self._retry_patch = self.strategy_page.collect_retry().to_dict()
        except Exception as exc:
            QMessageBox.warning(self, "重试策略无效", str(exc))
            self._select_page(self.strategy_page)
            return

        try:
            self._agent_patch = self.strategy_page.collect_agent().to_dict()
        except Exception as exc:
            QMessageBox.warning(self, "Agent 策略无效", str(exc))
            self._select_page(self.strategy_page)
            return

        try:
            ctx = self.strategy_page.collect_context()
            self._context_patch = {"context": ctx.to_dict()}
        except Exception as exc:
            QMessageBox.warning(self, "压缩阈值无效", str(exc))
            self._select_page(self.strategy_page)
            return

        try:
            prompts = self.instructions_page.collect()
            self._capability_patch = {
                "prompts": prompts.to_dict(),
                "capabilities": self.capabilities_page.collect_capabilities().to_dict(),
            }
        except Exception as exc:
            QMessageBox.warning(self, "能力配置无效", str(exc))
            self._select_page(self.capabilities_page)
            return

        try:
            prepared_channels = self.channel_service.prepare_for_save(
                self.channels_page.collect(),
                preferred_session_id=self.channels_page.get_preferred_session_id(),
            )
            self._preferred_channel_session_id = prepared_channels.preferred_session_id
            self._channels_patch = {
                "channels": [channel.to_dict() for channel in prepared_channels.channels],
            }
        except Exception as exc:
            QMessageBox.warning(self, "频道配置无效", str(exc))
            self._select_page(self.channels_page)
            return

        try:
            self._terminal_patch = dict(self.terminal_page.collect() or {})
        except Exception as exc:
            QMessageBox.warning(self, "终端配置无效", str(exc))
            self._select_page(self.general_page)
            return

        super().accept()

    def get_providers(self) -> List[Provider]:
        return list(self.providers or [])

    def build_update(self) -> AppSettingsUpdate:
        settings_patch = {
            "show_sidebar": bool(getattr(self._app_config, "show_sidebar", True)),
            "show_stats": self.get_show_stats(),
            "theme": self.get_theme(),
            "accent": self.get_accent(),
            "show_thinking": self.get_show_thinking(),
            "close_to_tray": self.get_close_to_tray(),
            "log_stream": self.get_log_stream(),
            "proxy_url": self.get_proxy_url(),
            "llm_timeout_seconds": self.get_llm_timeout_seconds(),
        }
        settings_patch.update(self.get_permission_settings())
        settings_patch.update(self.get_model_settings())
        settings_patch["agent"] = self.get_agent_settings()

        retry_patch = self.get_retry_settings()
        if retry_patch:
            settings_patch["retry"] = retry_patch

        settings_patch.update(self.get_context_settings())
        settings_patch.update(self.get_capability_settings())
        settings_patch.update(self.get_channel_settings())
        settings_patch.update(self.get_terminal_settings())

        return AppSettingsUpdate(
            providers=tuple(self.get_providers()),
            settings_patch=settings_patch,
            mcp_servers=tuple(self._mcp_servers),
            modes=tuple(self._modes),
            search_config=self.search_config,
        )

    def get_theme(self) -> str:
        return str(self._appearance_patch.get("theme") or self._app_config.theme)

    def get_accent(self) -> str:
        return str(self._appearance_patch.get("accent") or self._app_config.accent)

    def get_show_stats(self) -> bool:
        return bool(self._appearance_patch.get("show_stats", self._app_config.show_stats))

    def get_show_thinking(self) -> bool:
        return bool(self._appearance_patch.get("show_thinking", self._app_config.show_thinking))

    def get_close_to_tray(self) -> bool:
        return bool(self._appearance_patch.get("close_to_tray", self._app_config.close_to_tray))

    def get_log_stream(self) -> bool:
        return bool(self._appearance_patch.get("log_stream", self._app_config.log_stream))

    def get_proxy_url(self) -> str:
        return str(self._general_patch.get("proxy_url", self._app_config.proxy_url) or "")

    def get_llm_timeout_seconds(self) -> float:
        try:
            return float(self._general_patch.get("llm_timeout_seconds", self._app_config.llm_timeout_seconds))
        except Exception:
            return float(getattr(self._app_config, "llm_timeout_seconds", 600.0) or 600.0)

    def get_permission_settings(self) -> dict:
        return dict(self._permissions_patch or {})

    def get_auto_approve_settings(self) -> dict:
        # Compatibility for older tests/extensions that still call the old name.
        return self.get_permission_settings()

    def get_model_settings(self) -> dict:
        return dict(self._models_patch or {})

    def get_retry_settings(self) -> dict:
        return dict(self._retry_patch if hasattr(self, '_retry_patch') else {})

    def get_agent_settings(self) -> dict:
        return dict(self._agent_patch if hasattr(self, '_agent_patch') else {})

    def get_context_settings(self) -> dict:
        return dict(self._context_patch or {})

    def get_capability_settings(self) -> dict:
        return dict(self._capability_patch or {})

    def get_channel_settings(self) -> dict:
        return dict(self._channels_patch or {})

    def get_preferred_channel_session_id(self) -> str:
        return str(self._preferred_channel_session_id or "").strip()

    def get_terminal_settings(self) -> dict:
        return dict(self._terminal_patch or {})
