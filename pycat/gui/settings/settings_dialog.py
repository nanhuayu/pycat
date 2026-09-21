"""Settings dialog (thin container).

This dialog hosts application-wide pages under ``pycat.gui.settings.pages``.
"""

from __future__ import annotations

import logging
import json
from dataclasses import asdict, dataclass, is_dataclass
from typing import List

from PyQt6.QtCore import Qt, pyqtSignal, QSize, QTimer
from PyQt6.QtGui import QIcon, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QListWidget,
    QListWidgetItem,
    QStackedWidget,
    QPushButton,
    QFrame,
    QSizePolicy,
    QMessageBox,
    QScrollArea,
    QTabWidget,
    QWidget,
)

from pycat.core.app import AppSettingsUpdate
from pycat.core.app.services.channel import ChannelService
from pycat.core.tools.manager import ToolManager
from pycat.models.provider import Provider
from pycat.core.app.services.provider_catalog import ProviderCatalogService
from pycat.core.app.services.provider import ProviderService
from pycat.core.app.services.mode_catalog import ModeCatalogService
from pycat.models.contracts.config import AppConfig
from pycat.models.contracts.capability import CapabilitiesConfig
from pycat.models.search_config import SearchConfig
from pycat.gui.resources.page import ResourcePage

from pycat.gui.settings.pages import (
    ModelsPage,
    StrategyPage,
    PermissionsPage,
    ChannelsPage,
    CapabilitiesPage,
    ModesPage,
    AboutPage,
    AppearancePage,
    TerminalPage,
    OcrPage,
    SearchPage,
)
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.form_builder import FormSection
from pycat.gui.utils.theme import theme_tokens, resolve_theme
from pycat.gui.settings.pages.instructions_page import InstructionsPage
from pycat.gui.settings.pages.shortcuts_page import ShortcutsPage
from pycat.gui.settings.pages.automation_page import AutomationPage
from pycat.gui.settings.page_header import build_page_header
from pycat.gui.utils.window_geometry import apply_workbench_dialog_size
from pycat.gui.settings.components import SETTINGS_NAV_WIDTH


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


# Sentinel distinguishing "collector raised" from legitimately falsy results.
_COLLECT_FAILED = object()


# 页面 emoji 到统一图标的映射
_PAGE_ICON_MAP = {
    "ModelsPage": Icons.PAGE_MODELS,
    "StrategyPage": Icons.SLIDERS,
    "PermissionsPage": Icons.SHIELD,
    "AppearancePage": Icons.PAGE_APPEARANCE,
    "ChannelsPage": Icons.PAGE_CHANNELS,
    "TerminalPage": Icons.PAGE_TERMINAL_SETTINGS,
    "ResourcePage": Icons.TOOLS,
    "CapabilitiesPage": Icons.PAGE_CAPABILITIES,
    "ModesPage": Icons.PAGE_MODES,
    "SearchPage": Icons.PAGE_SEARCH,
    "AboutPage": Icons.PAGE_ABOUT,
    "OcrPage": Icons.IMAGE,
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
    save_requested = pyqtSignal(object)
    dirty_changed = pyqtSignal(bool)
    library_requested = pyqtSignal()
    project_instructions_requested = pyqtSignal()
    capture_requested = pyqtSignal()

    _DOMAIN_ORDER = ("providers", "app_settings", "mcp", "modes", "search")
    _DOMAIN_LABELS = {
        "providers": "服务商",
        "app_settings": "应用设置",
        "mcp": "MCP",
        "modes": "模式",
        "search": "搜索",
        "invalid": "无效配置",
    }

    def __init__(
        self,
        providers: List[Provider],
        current_settings: dict | None = None,
        provider_service: ProviderService | None = None,
        provider_catalog_service: ProviderCatalogService | None = None,
        mode_catalog_service: ModeCatalogService | None = None,
        search_config: SearchConfig | None = None,
        mcp_servers=(),
        mcp_server_provider=None,
        skill_service=None,
        extension_service=None,
        candidate_evaluator=None,
        channel_service: ChannelService | None = None,
        tool_manager: ToolManager | None = None,
        parent=None,
        work_dir: str | None = None,
        initial_page: str = "",
        selected_provider_id: str = "",
        embedded: bool = False,
        shell_choices=(),
    ):
        super().__init__(parent)
        self._embedded = embedded
        self._shell_choices = shell_choices
        if embedded:
            self.setWindowFlags(Qt.WindowType.Widget)
        self.providers = list(providers or [])
        self.current_settings = current_settings or {}
        self.work_dir = str(work_dir or "")
        self._initial_page = str(initial_page or "").strip().lower()
        self._selected_provider_id = str(selected_provider_id or "").strip()

        self.provider_service = provider_service or ProviderService()
        self.mode_catalog_service = mode_catalog_service or ModeCatalogService()
        if provider_catalog_service is None:
            raise ValueError("SettingsDialog requires ProviderCatalogService")
        self.provider_catalog_service = provider_catalog_service
        if channel_service is None:
            raise ValueError("SettingsDialog requires ChannelService")
        self.channel_service = channel_service
        self.tool_manager = tool_manager
        self._mcp_servers = tuple(mcp_servers)
        self._mcp_server_provider = mcp_server_provider
        self._skill_service = skill_service
        self._extension_service = extension_service
        self._candidate_evaluator = candidate_evaluator
        self.providers = self.provider_catalog_service.snapshot(self.providers)
        self.search_config = search_config or SearchConfig()
        self._app_config = AppConfig.from_dict(self.current_settings)

        self._appearance_patch: dict = {}
        self._general_patch: dict = {}
        self._shortcut_patch: dict = {}
        self._models_patch: dict = {}
        self._permissions_patch: dict = {}
        self._agent_patch: dict = {}
        self._context_patch: dict = {}
        self._capability_patch: dict = {}
        self._channels_patch: dict = {}
        self._terminal_patch: dict = {}
        self._ocr_patch: dict = {}
        self._modes = tuple()
        self._preferred_channel_session_id = ""
        self._baseline_fingerprints: dict[str, str] = {}
        self._pending_fingerprints: dict[str, str] = {}
        self._dirty_domain_cache: tuple[str, ...] = ()
        self._dirty_hint = False
        self._saving = False
        self._close_after_save = False
        self._allow_close = False
        self._collecting = False
        self._tracking_ready = False
        self._dirty_refresh_timer = QTimer(self)
        self._dirty_refresh_timer.setSingleShot(True)
        self._dirty_refresh_timer.timeout.connect(self._refresh_dirty_state)
        self._dirty_refresh_timer.destroyed.connect(self._disable_dirty_tracking)

        self._setup_ui()
        self._connect_dirty_tracking()
        initial = self._current_fingerprints()
        self._baseline_fingerprints = dict(initial or {})
        self._tracking_ready = True
        self._refresh_dirty_state()

    def _configure_automation(self, preset: str) -> None:
        self._select_page(self.mcp_page)
        self.mcp_page.show_market("agent-browser" if preset == "browser" else "cua-driver")

    def _setup_ui(self) -> None:
        self.setWindowTitle("设置")
        self.setObjectName("settings_dialog")
        self.setModal(not self._embedded)
        if not self._embedded:
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

        brand = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(Icons.brand().pixmap(28, 28))
        brand.addWidget(logo)
        name = QLabel("PyCat")
        name.setObjectName("settings_brand")
        brand.addWidget(name)
        brand.addStretch()
        back = QPushButton()
        back.setObjectName("settings_back")
        back.setIcon(Icons.get_muted(Icons.ARROW_LEFT))
        back.setFixedSize(30, 30)
        back.setToolTip("返回会话")
        back.clicked.connect(self.reject)
        brand.addWidget(back)
        sidebar_layout.addLayout(brand)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜索设置…")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._filter_pages)
        sidebar_layout.addWidget(self.search_input)

        self.page_list = QListWidget()
        self.page_list.setObjectName("settings_nav")
        self.page_list.setIconSize(QSize(Icons.SIZE_SETTINGS_NAV, Icons.SIZE_SETTINGS_NAV))
        self.page_list.setSpacing(1)
        self.page_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.page_list.currentItemChanged.connect(self._change_page)
        sidebar_layout.addWidget(self.page_list, 1)

        layout.addWidget(sidebar)
        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.content = QStackedWidget()
        self.content.setObjectName("settings_content")
        body.addWidget(self.content, 1)
        footer = QFrame()
        self.save_footer = footer
        footer.setObjectName("settings_footer")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(18, 8, 18, 8)
        self.status_label = QLabel("")
        self.status_label.setObjectName("settings_save_status")
        self.status_label.setProperty("muted", True)
        self.status_label.setWordWrap(True)
        self.status_label.setVisible(False)
        footer_layout.addWidget(self.status_label, 1)
        footer_layout.addStretch(1)

        self.save_btn = QPushButton("保存更改")
        self.save_btn.setObjectName("settings_action_btn")
        self.save_btn.setProperty("primary", True)
        self.save_btn.setIcon(Icons.get(Icons.SAVE, color=theme_tokens(resolve_theme(self)).color("on_primary")))
        self.save_btn.clicked.connect(lambda _checked=False: self.request_save())
        footer_layout.addWidget(self.save_btn)
        body.addWidget(footer)

        self._save_shortcut = QShortcut(QKeySequence.StandardKey.Save, self)
        self._save_shortcut.activated.connect(self.request_save)

        layout.addLayout(body, 1)

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
            memory_char_limit=self._app_config.memory_char_limit,
            user_memory_char_limit=self._app_config.user_memory_char_limit,
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
        self.mcp_page = ResourcePage(kind="mcp", mcp_servers=self._mcp_servers,
            work_dir=self.work_dir, extension_service=self._extension_service,
            reload_provider=self._mcp_server_provider,
            connection_tester=self.tool_manager.test_server_connection if self.tool_manager else None)
        self.skills_page = ResourcePage(kind="skill", work_dir=self.work_dir,
            skill_service=self._skill_service, extension_service=self._extension_service,
            servers_provider=self.mcp_page.installed.collect_servers,
            candidate_evaluator=self._candidate_evaluator)
        self.mcp_page.changed.connect(self.mark_dirty)
        ocr_status = None
        if self.tool_manager is not None:
            try:
                ocr_status = self.tool_manager.ocr_status()
            except Exception as exc:
                logger.debug("Failed to inspect local OCR runtime: %s", exc)
        self.ocr_page = OcrPage(self._app_config.ocr, status=ocr_status, providers=self.providers,
                                capability=self._app_config.capabilities.capability("ocr"))
        self.search_page = SearchPage(self.search_config)
        self.general_page = AppearancePage(
            theme=self._app_config.theme,
            accent=self._app_config.accent,
            show_thinking=self._app_config.show_thinking,
            close_to_tray=self._app_config.close_to_tray,
            log_stream=self._app_config.log_stream,
            proxy_url=self._app_config.proxy_url,
            llm_timeout_seconds=float(getattr(self._app_config, "llm_timeout_seconds", 600.0) or 600.0),
        )
        self.appearance_page = self.general_page
        self.instructions_page = InstructionsPage(self._app_config.prompts)
        self.terminal_page = TerminalPage(self._app_config.shell, shell_choices=self._shell_choices)
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

        self.network_page = QWidget()
        network_layout = QVBoxLayout(self.network_page)
        network_layout.setContentsMargins(16, 16, 16, 16)
        network_layout.addWidget(self.appearance_page.network_group)
        network_layout.addStretch()
        self.memory_page = QWidget()
        memory_layout = QVBoxLayout(self.memory_page)
        memory_layout.setContentsMargins(16, 16, 16, 16)
        memory_layout.setSpacing(12)
        memory_layout.addWidget(build_page_header("记忆与资料", "查看来源、管理记忆和阅读资料。"))
        library = FormSection("资料与上下文")
        for title, detail, action, callback in (
            ("资料与记忆", "阅读文件、审核项目记忆与用户偏好。", "打开", self.library_requested.emit),
            ("指令与来源", "管理全局指令和项目 AGENTS.md。", "查看", lambda: self._select_page(self.instructions_page)),
            ("压缩策略", "分别设置工具内容压缩与上下文压缩。", "设置", lambda: self._select_page(self.strategy_page)),
        ):
            label = QLabel(f"{title}\n{detail}")
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            button = QPushButton(action)
            button.setFixedWidth(64)
            button.setAccessibleName(f"{action}{title}")
            button.clicked.connect(callback)
            library.form.addRow(label, button)
        memory_layout.addWidget(library.group)
        memory_layout.addStretch()
        sources = QPushButton("查看项目 AGENTS.md")
        sources.setEnabled(bool(self.work_dir))
        sources.setToolTip(self.work_dir or "当前未选择项目")
        sources.clicked.connect(self.project_instructions_requested)
        self.instructions_page.layout().insertWidget(self.instructions_page.layout().count() - 1, sources)

        self.shortcuts_page = ShortcutsPage(self._app_config.shortcuts)
        self.automation_page = AutomationPage()
        self.automation_page.capture_requested.connect(self.capture_requested.emit)
        self.automation_page.configure_requested.connect(self._configure_automation)
        self.automation_page.shortcuts_requested.connect(lambda: self._select_page(self.shortcuts_page))
        self._page_specs = [
            SettingsPageSpec("general", "外观", "通用", self.general_page),
            SettingsPageSpec("shortcuts", "快捷键", "通用", self.shortcuts_page),
            SettingsPageSpec("models", "模型与服务", "模型与服务", self.models_page, False),
            SettingsPageSpec("modes", "模式", "运行与权限", self.modes_page, False),
            SettingsPageSpec("permissions", "权限", "运行与权限", self.permissions_page, False),
            SettingsPageSpec("strategy", "策略", "运行与权限", self.strategy_page),
            SettingsPageSpec("instructions", "指令与来源", "运行与权限", self.instructions_page),
            SettingsPageSpec("skills", "技能 Skills", "工具与能力", self.skills_page, False),
            SettingsPageSpec("mcp", "MCP", "工具与能力", self.mcp_page, False),
            SettingsPageSpec("capabilities", "能力", "工具与能力", self.capabilities_page, False),
            SettingsPageSpec("search", "搜索", "工具与能力", self.search_page),
            SettingsPageSpec('automation', '电脑与浏览器', '工具与能力', self.automation_page),
            SettingsPageSpec("ocr", "OCR", "工具与能力", self.ocr_page),
            SettingsPageSpec("memory", "记忆与资料", "记忆与资料", self.memory_page),
            SettingsPageSpec("channels", "消息通道", "消息通道", self.channels_page, False),
            SettingsPageSpec("network", "网络与诊断", "高级与数据", self.network_page),
            SettingsPageSpec("terminal", "终端", "高级与数据", self.terminal_page),
            SettingsPageSpec("about", "关于", "高级与数据", self.about_page),
        ]
        self._nav_rows_by_page: dict[object, int] = {}
        self._page_tabs = {}
        groups = list(dict.fromkeys(spec.group for spec in self._page_specs))
        for page_index, group in enumerate(groups):
            specs = [spec for spec in self._page_specs if spec.group == group]
            if len(specs) == 1:
                spec = specs[0]
                self.content.addWidget(self._wrap_page(spec.page) if spec.scrollable else spec.page)
            else:
                group_page = QWidget()
                group_layout = QVBoxLayout(group_page)
                group_layout.setContentsMargins(16, 16, 16, 8)
                group_layout.setSpacing(12)
                group_layout.addWidget(build_page_header(group))
                tabs = QTabWidget()
                tabs.setObjectName("settings_subtabs")
                group_layout.addWidget(tabs, 1)
                for spec in specs:
                    if header := spec.page.findChild(QWidget, "settings_page_header"):
                        header.hide()
                    index = tabs.addTab(self._wrap_page(spec.page) if spec.scrollable else spec.page, spec.title)
                    self._page_tabs[spec.page] = (tabs, index)
                tabs.currentChanged.connect(self._update_save_footer)
                self.content.addWidget(group_page)
            item = QListWidgetItem(_get_page_icon(specs[0].page), group)
            item.setData(PAGE_INDEX_ROLE, page_index)
            item.setData(PAGE_KEY_ROLE, specs[0].key)
            self.page_list.addItem(item)
            for spec in specs:
                self._nav_rows_by_page[spec.page] = page_index

    def _filter_pages(self, text: str):
        query = text.strip().casefold()
        for row in range(self.page_list.count()):
            item = self.page_list.item(row)
            specs = [s for s in self._page_specs if self._nav_rows_by_page.get(s.page) == row]
            words = " ".join([item.text()] + [s.title + " " + " ".join(label.text() for label in s.page.findChildren(QLabel)) for s in specs])
            item.setHidden(bool(query) and query not in words.casefold())

    def _sync_provider_dependent_pages(self) -> None:
        providers = self.provider_catalog_service.snapshot(self.models_page.providers)
        self.capabilities_page.set_providers(providers)
        self.ocr_page.set_providers(providers)
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
        self._update_save_footer()

    def _select_page(self, page: object) -> None:
        row = self._nav_rows_by_page.get(page, -1)
        if row >= 0:
            self.page_list.item(row).setHidden(False)
            self.page_list.setCurrentRow(row)
            if page in self._page_tabs:
                tabs, index = self._page_tabs[page]
                tabs.setCurrentIndex(index)

    def focus_page(self, key: str = "", *, selected_provider_id: str = "") -> None:
        """Focus an existing settings window without rebuilding its pages."""
        clean = str(key or "").strip().lower()
        if clean:
            spec = next(
                (
                    item
                    for item in self._page_specs
                    if clean in {item.key.lower(), item.title.lower()}
                ),
                None,
            )
            if spec is not None:
                self._select_page(spec.page)
        provider_id = str(selected_provider_id or "").strip()
        if provider_id:
            self.models_page.select_provider(provider_id)

        self.show()
        self.raise_()
        self.activateWindow()

    def _collect_form_values(self, *, prepare_channels: bool, show_errors: bool) -> bool:
        self._collecting = True
        try:
            return self._collect_form_values_impl(
                prepare_channels=prepare_channels,
                show_errors=show_errors,
            )
        finally:
            self._collecting = False

    def _collect_form_values_impl(self, *, prepare_channels: bool, show_errors: bool) -> bool:
        modes = self._collect_section(
            "模式配置无效", self.modes_page, self.modes_page.collect_modes, show_errors
        )
        if modes is _COLLECT_FAILED:
            return False
        self._modes = tuple(modes)

        providers = self._collect_section(
            "服务商配置无效", self.models_page, self.models_page.get_providers, show_errors
        )
        if providers is _COLLECT_FAILED:
            return False
        self.providers = providers

        models_patch = self._collect_section(
            "默认模型无效",
            self.models_page,
            lambda: {
                "default_chat_model": self.models_page.collect_default_chat_model(),
                "default_auxiliary_model": self.models_page.collect_default_auxiliary_model(),
            },
            show_errors,
        )
        if models_patch is _COLLECT_FAILED:
            return False
        self._models_patch = models_patch

        mcp_servers = self._collect_section(
            "MCP 配置无效", self.mcp_page, self.mcp_page.installed.collect_servers, show_errors)
        if mcp_servers is _COLLECT_FAILED:
            if show_errors:
                self.mcp_page.view_tabs.setCurrentIndex(0)
                self.mcp_page.installed.editor.browser.show_detail()
            return False
        self._mcp_servers = tuple(mcp_servers)

        search_config = self._collect_section(
            "搜索配置无效", self.search_page, self.search_page.collect, show_errors
        )
        if search_config is _COLLECT_FAILED:
            return False
        self.search_config = search_config

        appearance_patch = self._collect_section(
            "通用配置无效", self.general_page, self.appearance_page.collect, show_errors
        )
        if appearance_patch is _COLLECT_FAILED:
            return False
        self._appearance_patch = dict(appearance_patch or {})
        self._general_patch = dict(appearance_patch or {})
        shortcuts = self._collect_section("快捷键配置无效", self.shortcuts_page, self.shortcuts_page.collect, show_errors)
        if shortcuts is _COLLECT_FAILED:
            return False
        self._shortcut_patch = shortcuts

        perms = self._collect_section(
            "权限配置无效", self.permissions_page, self.permissions_page.collect, show_errors
        )
        if perms is _COLLECT_FAILED:
            return False
        self._permissions_patch = {"permissions": perms.to_dict()}

        retry_config = self._collect_section(
            "重试策略无效", self.strategy_page, self.strategy_page.collect_retry, show_errors
        )
        if retry_config is _COLLECT_FAILED:
            return False
        self._retry_patch = retry_config.to_dict()

        agent_config = self._collect_section(
            "Agent 策略无效", self.strategy_page, self.strategy_page.collect_agent, show_errors
        )
        if agent_config is _COLLECT_FAILED:
            return False
        self._agent_patch = agent_config.to_dict()

        ctx = self._collect_section(
            "压缩阈值无效", self.strategy_page, self.strategy_page.collect_context, show_errors
        )
        if ctx is _COLLECT_FAILED:
            return False
        self._context_patch = {"context": ctx.to_dict()}

        prompts = self._collect_section(
            "指令配置无效", self.instructions_page, self.instructions_page.collect, show_errors
        )
        if prompts is _COLLECT_FAILED:
            return False
        capabilities = self._collect_section(
            "能力配置无效",
            self.capabilities_page,
            self.capabilities_page.collect_capabilities,
            show_errors,
        )
        if capabilities is _COLLECT_FAILED:
            return False
        capabilities = CapabilitiesConfig(capabilities=(*capabilities.capabilities, self.ocr_page.collect_capability()))
        self._capability_patch = {
            "prompts": prompts.to_dict(),
            "capabilities": capabilities.to_dict(),
        }

        try:
            channels = tuple(self.channels_page.collect())
            preferred_session_id = self.channels_page.get_preferred_session_id()
            if prepare_channels:
                prepared_channels = self.channel_service.prepare_for_save(
                    channels,
                    preferred_session_id=preferred_session_id,
                )
                self._preferred_channel_session_id = prepared_channels.preferred_session_id
                channels = prepared_channels.channels
            else:
                self._preferred_channel_session_id = str(preferred_session_id or "").strip()
            self._channels_patch = {"channels": [channel.to_dict() for channel in channels]}
        except Exception as exc:
            return self._validation_failure("频道配置无效", exc, self.channels_page, show_errors)

        terminal_patch = self._collect_section(
            "终端配置无效", self.general_page, self.terminal_page.collect, show_errors
        )
        if terminal_patch is _COLLECT_FAILED:
            return False
        self._terminal_patch = dict(terminal_patch or {})

        ocr_config = self._collect_section(
            "OCR 配置无效", self.ocr_page, self.ocr_page.collect, show_errors
        )
        if ocr_config is _COLLECT_FAILED:
            return False
        self._ocr_patch = {"ocr": ocr_config.to_dict()}

        return True

    def _collect_section(self, title: str, page: object, collector, show_errors: bool):
        """Run one page collector; failures route through validation UX."""
        try:
            return collector()
        except Exception as exc:
            self._validation_failure(title, exc, page, show_errors)
            return _COLLECT_FAILED

    def _validation_failure(self, title: str, error: Exception, page: object, show_errors: bool) -> bool:
        if show_errors:
            QMessageBox.warning(self, title, str(error))
            self._select_page(page)
        return False

    def collect_update(self) -> AppSettingsUpdate | None:
        """Validate the current draft and return one immutable save command."""

        if not self._collect_form_values(prepare_channels=True, show_errors=True):
            return None
        return self.build_update()

    def request_save(self, *, close_after: bool = False) -> None:
        if self._saving:
            return
        if not close_after and not self.is_dirty():
            return
        update = self.collect_update()
        if update is None:
            return
        self._pending_fingerprints = self._fingerprints(update)
        self._close_after_save = bool(close_after)
        self._set_saving(True)
        self.save_requested.emit(update)

    def accept(self) -> None:
        """Treat Enter/default acceptance as Save, not as implicit close."""

        self.request_save()

    def reject(self) -> None:
        self.request_close()

    def request_close(self) -> None:
        if self._saving:
            return
        if not self.is_dirty():
            self.close_without_prompt()
            return
        decision = self._confirm_close()
        if decision == "save":
            self.request_save(close_after=True)
        elif decision == "discard":
            self.close_without_prompt()

    def _confirm_close(self) -> str:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("未保存的设置")
        box.setText("设置中有未保存的更改。")
        box.setInformativeText("保存后关闭，或放弃这些配置草稿？")
        box.setStandardButtons(
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel
        )
        save = box.button(QMessageBox.StandardButton.Save)
        discard = box.button(QMessageBox.StandardButton.Discard)
        cancel = box.button(QMessageBox.StandardButton.Cancel)
        if save is not None:
            save.setText("保存并关闭")
        if discard is not None:
            discard.setText("放弃更改")
        if cancel is not None:
            cancel.setText("继续编辑")
        result = box.exec()
        if result == QMessageBox.StandardButton.Save:
            return "save"
        if result == QMessageBox.StandardButton.Discard:
            return "discard"
        return "cancel"

    def close_without_prompt(self) -> None:
        self._prepare_to_close()
        self._allow_close = True
        super().reject()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._tracking_ready or self._saving or self._collecting:
            return
        # Defensive self-heal: if this dialog instance is ever resurrected
        # (e.g. presenter refocus after a close path that skipped reject()),
        # dirty tracking must resume instead of silently ignoring saves.
        self._tracking_ready = True
        self._baseline_fingerprints = dict(self._current_fingerprints() or {})
        self._dirty_domain_cache = ()
        self._refresh_dirty_state()

    def closeEvent(self, event) -> None:
        if self._allow_close:
            self._prepare_to_close()
            event.accept()
            return
        if self._saving:
            event.ignore()
            return
        if not self.is_dirty():
            # Accept the close, but funnel through reject() so finished()
            # fires and the presenter releases this dialog. Accepting alone
            # would leave a resurrectable "zombie" whose dirty tracking is
            # off, making later saves look silently ignored.
            event.accept()
            self.close_without_prompt()
            return
        event.ignore()
        self.request_close()

    def apply_save_result(self, result: object) -> None:
        """Rebase successful domains while retaining failed-domain drafts."""

        self._set_saving(False)
        current = self._current_fingerprints() or dict(self._pending_fingerprints)
        saved_domains = tuple(getattr(result, "saved_domains", ()) or ())
        snapshot = getattr(result, "snapshot", None)
        if snapshot is not None:
            if "app_settings" in saved_domains:
                self._app_config = AppConfig.from_dict(getattr(snapshot, "app_settings", {}) or {})
            if "providers" in saved_domains:
                self.providers = list(getattr(snapshot, "providers", ()) or ())
            if "modes" in saved_domains:
                self._modes = tuple(getattr(snapshot, "modes", ()) or ())
            if "search" in saved_domains:
                self.search_config = getattr(snapshot, "search_config", self.search_config)
        for domain in saved_domains:
            if domain in current:
                self._baseline_fingerprints[domain] = current[domain]
            elif domain in self._pending_fingerprints:
                self._baseline_fingerprints[domain] = self._pending_fingerprints[domain]

        if "mcp" in saved_domains:
            self.mcp_page.installed.mark_saved()
        self._dirty_hint = False
        self._refresh_dirty_state()
        failed_domains = tuple(getattr(result, "failed_domains", ()) or ())
        failed_stages = tuple(getattr(result, "failed_stages", ()) or ())
        if failed_domains:
            labels = [self._DOMAIN_LABELS.get(domain, domain) for domain in failed_domains]
            self._set_status("未保存：" + "、".join(labels), error=True)
        elif failed_stages:
            self._set_status("设置已保存，但运行时尚未完全应用。", error=True)
        else:
            self._set_status("设置已保存。")

        should_close = self._close_after_save and not failed_domains
        self._close_after_save = False
        self._pending_fingerprints = {}
        if should_close:
            self.close_without_prompt()

    def apply_save_error(self, error: Exception) -> None:
        self._set_saving(False)
        self._close_after_save = False
        self._set_status(f"保存失败：{error}", error=True)
        self._refresh_dirty_state()

    def is_saving(self) -> bool:
        return self._saving

    def is_dirty(self) -> bool:
        self._refresh_dirty_state()
        return bool(self._dirty_domain_cache)

    def dirty_domains(self) -> tuple[str, ...]:
        self._refresh_dirty_state()
        return self._dirty_domain_cache

    def mark_dirty(self) -> None:
        if not self._tracking_ready or self._collecting or self._saving:
            return
        self._dirty_hint = True
        try:
            self._dirty_refresh_timer.start(0)
        except RuntimeError:
            self._disable_dirty_tracking()

    def _prepare_to_close(self) -> None:
        self.mcp_page.cancel_pending()
        self.skills_page.cancel_pending()
        self._tracking_ready = False
        self._stop_dirty_refresh_timer()

    def _disable_dirty_tracking(self, _object=None) -> None:
        self._tracking_ready = False

    def _stop_dirty_refresh_timer(self) -> None:
        try:
            self._dirty_refresh_timer.stop()
        except RuntimeError:
            self._disable_dirty_tracking()

    def _set_saving(self, saving: bool) -> None:
        self._saving = bool(saving)
        self.page_list.setEnabled(not self._saving)
        self.content.setEnabled(not self._saving)
        if self._saving:
            self.save_btn.setEnabled(False)
            self._set_status("正在保存设置...")
        else:
            self._refresh_dirty_state()

    def _set_status(self, text: str, *, error: bool = False) -> None:
        value = str(text or "").strip()
        self.status_label.setText(value)
        self.status_label.setProperty("error", bool(error))
        self.status_label.setVisible(bool(value))
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _refresh_dirty_state(self) -> None:
        if not self._tracking_ready or self._collecting or self._saving:
            return
        self._stop_dirty_refresh_timer()
        if not self._tracking_ready:
            return
        was_dirty = bool(self._dirty_domain_cache)
        current = self._current_fingerprints()
        if current is None:
            dirty = ("invalid",)
        else:
            dirty = tuple(
                domain
                for domain in self._DOMAIN_ORDER
                if current.get(domain) != self._baseline_fingerprints.get(domain)
            )
        self._dirty_domain_cache = dirty
        now_dirty = bool(dirty)
        self.save_btn.setEnabled(now_dirty)
        self._update_save_footer()
        if now_dirty and not self.status_label.property("error"):
            self._set_status("有未保存的更改")
        elif not now_dirty and self.status_label.text() == "有未保存的更改":
            self._set_status("")
        if was_dirty != now_dirty:
            self.dirty_changed.emit(now_dirty)
        self._dirty_hint = False

    def _update_save_footer(self):
        entry = getattr(self, "_page_tabs", {}).get(getattr(self, "skills_page", None))
        on_skills = bool(entry and entry[0].currentIndex() == entry[1] and self.page_list.currentRow() == self._nav_rows_by_page.get(self.skills_page))
        self.save_footer.setVisible(not on_skills or bool(self._dirty_domain_cache) or self._saving)

    def _current_fingerprints(self) -> dict[str, str] | None:
        if not self._collect_form_values(prepare_channels=False, show_errors=False):
            return None
        return self._fingerprints(self.build_update())

    @staticmethod
    def _fingerprints(update: AppSettingsUpdate) -> dict[str, str]:
        def payload(value):
            if hasattr(value, "to_dict"):
                return value.to_dict()
            if is_dataclass(value):
                return asdict(value)
            return value

        payloads = {
            "providers": [payload(provider) for provider in update.providers],
            "app_settings": dict(update.settings_patch or {}),
            "mcp": [payload(server) for server in update.mcp_servers],
            "modes": [payload(mode) for mode in update.modes],
            "search": payload(update.search_config) if update.search_config is not None else None,
        }
        return {
            domain: json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            for domain, value in payloads.items()
        }

    def _connect_dirty_tracking(self) -> None:
        callback = lambda *_args: self.mark_dirty()
        for widget in self.findChildren(QLineEdit):
            widget.textChanged.connect(callback)
        for widget_type in (QTextEdit, QPlainTextEdit):
            for widget in self.findChildren(widget_type):
                widget.textChanged.connect(callback)
        for widget in self.findChildren(QComboBox):
            widget.currentIndexChanged.connect(callback)
            if widget.isEditable():
                widget.currentTextChanged.connect(callback)
        for widget_type in (QSpinBox, QDoubleSpinBox):
            for widget in self.findChildren(widget_type):
                widget.valueChanged.connect(callback)
        for widget in self.findChildren(QAbstractButton):
            if widget.isCheckable():
                widget.toggled.connect(callback)

        seen_models: set[int] = set()
        for view in self.findChildren(QAbstractItemView):
            model = view.model()
            if model is None or id(model) in seen_models:
                continue
            seen_models.add(id(model))
            model.dataChanged.connect(callback)
            model.rowsInserted.connect(callback)
            model.rowsRemoved.connect(callback)
            model.modelReset.connect(callback)
        self.providers_changed.connect(callback)

    def get_providers(self) -> List[Provider]:
        return list(self.providers or [])

    def build_update(self) -> AppSettingsUpdate:
        settings_patch = {
            "show_sidebar": bool(getattr(self._app_config, "show_sidebar", True)),
            "show_stats": self.get_show_stats(),
            "theme": self.get_theme(),
            "shortcuts": dict(self._shortcut_patch),
            "accent": self.get_accent(),
            "show_thinking": self.get_show_thinking(),
            "close_to_tray": self.get_close_to_tray(),
            "log_stream": self.get_log_stream(),
            "memory_char_limit": int(self.strategy_page.memory_limit_combo.currentText()),
            "user_memory_char_limit": int(self.strategy_page.user_memory_limit_combo.currentText()),
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
        settings_patch.update(self.get_ocr_settings())

        return AppSettingsUpdate(
            providers=tuple(self.get_providers()),
            settings_patch=settings_patch,
            mcp_servers=self._mcp_servers,
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

    def get_ocr_settings(self) -> dict:
        return dict(self._ocr_patch or {})
