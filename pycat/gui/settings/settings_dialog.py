"""Settings dialog (thin container).

This dialog hosts application-wide pages under ``pycat.gui.settings.pages``.
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from typing import List

from PyQt6.QtCore import QT_TRANSLATE_NOOP, QCoreApplication, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from pycat.core.app.services.channel import ChannelService
from pycat.core.app.services.mode_catalog import ModeCatalogService
from pycat.core.app.services.provider import ProviderService
from pycat.core.app.services.provider_catalog import ProviderCatalogService
from pycat.core.app.state import AppSettingsUpdate
from pycat.core.tools.manager import ToolManager
from pycat.gui.resources.page import ResourcePage
from pycat.gui.settings.components import SETTINGS_NAV_WIDTH
from pycat.gui.settings.page_header import build_page_header
from pycat.gui.settings.pages import (
    AboutPage,
    AppearancePage,
    CapabilitiesPage,
    ChannelsPage,
    ModelsPage,
    ModesPage,
    OcrPage,
    PermissionsPage,
    SearchPage,
    StrategyPage,
    TerminalPage,
)
from pycat.gui.settings.pages.automation_page import AutomationPage
from pycat.gui.settings.pages.instructions_page import InstructionsPage
from pycat.gui.settings.pages.shortcuts_page import ShortcutsPage
from pycat.gui.utils.form_builder import FormSection
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.window_geometry import apply_workbench_dialog_size
from pycat.gui.widgets.themed_line_edit import ThemedLineEdit
from pycat.models.contracts.capability import CapabilitiesConfig
from pycat.models.contracts.config import AppConfig
from pycat.models.provider import Provider
from pycat.models.search_config import SearchConfig

logger = logging.getLogger(__name__)


PAGE_INDEX_ROLE = Qt.ItemDataRole.UserRole + 1
PAGE_KEY_ROLE = Qt.ItemDataRole.UserRole + 2
NAV_GROUP_ROLE = Qt.ItemDataRole.UserRole + 3


@dataclass(frozen=True)
class SettingsPageSpec:
    key: str
    title: str
    group: str
    scrollable: bool = True
    keywords: str = ""
    edits_config: bool = True


# Sentinel distinguishing "collector raised" from legitimately falsy results.
_COLLECT_FAILED = object()


# Stable identities are independent of labels and of the first page's class.
_GROUPS = (
    ("general", QT_TRANSLATE_NOOP('SettingsDialog', "通用"), Icons.SLIDERS),
    ("models", QT_TRANSLATE_NOOP('SettingsDialog', "模型"), Icons.PAGE_MODELS),
    ("runtime", QT_TRANSLATE_NOOP('SettingsDialog', "运行与权限"), Icons.SHIELD),
    ("tools", QT_TRANSLATE_NOOP('SettingsDialog', "工具"), Icons.TOOLS),
    ("memory", QT_TRANSLATE_NOOP('SettingsDialog', "记忆与资料"), Icons.BOOKS),
    ("channels", QT_TRANSLATE_NOOP('SettingsDialog', "消息通道"), Icons.PLUG),
    ("advanced", QT_TRANSLATE_NOOP('SettingsDialog', "高级"), Icons.SETTINGS),
)


class SettingsDialog(QDialog):
    """Thin container dialog."""

    providers_changed = pyqtSignal()
    page_created = pyqtSignal(str, object)
    save_requested = pyqtSignal(object)
    dirty_changed = pyqtSignal(bool)
    library_requested = pyqtSignal()
    project_instructions_requested = pyqtSignal()
    capture_requested = pyqtSignal()

    _DOMAIN_ORDER = ("providers", "app_settings", "mcp", "modes", "search")
    _DOMAIN_LABELS = {
        "providers": QT_TRANSLATE_NOOP('SettingsDialog', "服务商"),
        "app_settings": QT_TRANSLATE_NOOP('SettingsDialog', "应用设置"),
        "mcp": "MCP",
        "modes": QT_TRANSLATE_NOOP('SettingsDialog', "模式"),
        "search": QT_TRANSLATE_NOOP('SettingsDialog', "搜索"),
        "invalid": QT_TRANSLATE_NOOP('SettingsDialog', "无效配置"),
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
        modes=None,
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

        # Unvisited forms retain the typed snapshot; saving never constructs them.
        self._settings_patch = {key: value for key, value in self._app_config.to_dict().items()
                                if key not in {"splitter_sizes", "chat_splitter_sizes", "main_window_size"}}
        self._modes = tuple(self.mode_catalog_service.load() if modes is None else modes)
        self._pages: dict[str, QWidget] = {}
        self._navigating = False
        self._preferred_channel_session_id = ""
        self._baseline_fingerprints: dict[str, str] = {}
        self._pending_fingerprints: dict[str, str] = {}
        self._dirty_domain_cache: tuple[str, ...] = ()
        self._dirty_hint = False
        self._saving = False
        self._status_kind = ""
        self._close_after_save = False
        self._allow_close = False
        self._collecting = False
        self._tracking_ready = False
        self._dirty_refresh_timer = QTimer(self)
        self._dirty_refresh_timer.setSingleShot(True)
        self._dirty_refresh_timer.timeout.connect(self._refresh_dirty_state)
        self._dirty_refresh_timer.destroyed.connect(self._disable_dirty_tracking)

        self._setup_ui()
        self.providers_changed.connect(self.mark_dirty)
        initial = self._current_fingerprints()
        self._baseline_fingerprints = dict(initial or {})
        self._tracking_ready = True
        self._refresh_dirty_state()

    def _configure_automation(self, preset: str) -> None:
        self._select_page(self.page("mcp"))
        self.page("mcp").show_market("agent-browser" if preset == "browser" else "cua-driver")

    def _setup_ui(self) -> None:
        self.setWindowTitle(QCoreApplication.translate('SettingsDialog', "设置"))
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
        back.setToolTip(QCoreApplication.translate('SettingsDialog', "返回会话"))
        back.setAccessibleName(QCoreApplication.translate('SettingsDialog', "返回会话"))
        back.clicked.connect(self.reject)
        brand.addWidget(back)
        sidebar_layout.addLayout(brand)
        self.search_input = ThemedLineEdit()
        self.search_input.setPlaceholderText(QCoreApplication.translate('SettingsDialog', "搜索设置…"))
        self.search_input.setAccessibleName(QCoreApplication.translate('SettingsDialog', "搜索设置"))
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

        self.save_btn = QPushButton(QCoreApplication.translate('SettingsDialog', "保存更改"))
        self.save_btn.setObjectName("settings_action_btn")
        self.save_btn.setProperty("primary", True)
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
        self._select_page(initial.key)
        if self._selected_provider_id:
            self.page("models").select_provider(self._selected_provider_id)

    def _apply_initial_size(self) -> None:
        apply_workbench_dialog_size(self)

    @property
    def created_pages(self) -> dict[str, QWidget]:
        """Read-only snapshot of materialized views; inspecting it does no work."""
        return dict(self._pages)

    def page(self, key: str) -> QWidget:
        """Build a view once and retain its draft for this dialog's lifetime."""
        if key in self._pages:
            return self._pages[key]
        spec = next(item for item in self._page_specs if item.key == key)
        page = self._create_page(key)
        self._pages[key] = page
        host = self._page_hosts[key]
        host.layout().addWidget(self._wrap_page(page) if spec.scrollable else page)
        if key in self._page_tabs:
            if header := page.findChild(QWidget, "settings_page_header"):
                header.hide()
        if spec.edits_config:
            # A form may normalize its initial display (e.g. default shortcuts).
            # Rebase only this page's owned fields, preserving all other drafts.
            if self._collect_form_values_impl(prepare_channels=False, show_errors=False, only_page=key):
                self._rebase_new_page(key)
            if key != "network":  # These controls already belong to AppearancePage.
                self._connect_dirty_tracking(page)
        self.page_created.emit(key, page)
        return page

    def _create_page(self, key: str) -> QWidget:
        config = self._app_config
        if key == "models":
            page = ModelsPage(self._draft_providers(), default_chat_model=config.default_chat_model,
                default_auxiliary_model=config.default_auxiliary_model,
                provider_service=self.provider_service, provider_catalog_service=self.provider_catalog_service)
            page.providers_changed.connect(self._sync_provider_dependent_pages)
            page.providers_changed.connect(self.providers_changed)
            return page
        if key == "strategy":
            return StrategyPage(agent=config.agent, retry=config.retry, context=config.context)
        if key == "permissions":
            return PermissionsPage(config.permissions, capabilities=config.capabilities, tool_manager=self.tool_manager)
        if key == "channels":
            return ChannelsPage(config.channels, channel_service=self.channel_service)
        if key == "mcp":
            page = ResourcePage(kind="mcp", mcp_servers=self._mcp_servers, work_dir=self.work_dir,
                extension_service=self._extension_service, reload_provider=self._mcp_server_provider,
                connection_tester=self.tool_manager.test_server_connection if self.tool_manager else None)
            page.changed.connect(self.mark_dirty)
            return page
        if key == "skills":
            return ResourcePage(kind="skill", work_dir=self.work_dir, skill_service=self._skill_service,
                extension_service=self._extension_service, servers_provider=self._draft_mcp_servers,
                candidate_evaluator=self._candidate_evaluator)
        if key == "ocr":
            status = None
            if self.tool_manager is not None:
                try:
                    status = self.tool_manager.ocr_status()
                except Exception as exc:
                    logger.debug("Failed to inspect local OCR runtime: %s", exc)
            return OcrPage(config.ocr, status=status, providers=self._draft_providers(),
                           capability=config.capabilities.capability("ocr"))
        if key == "search":
            return SearchPage(self.search_config)
        if key == "general":
            page = AppearancePage(theme=config.theme, accent=config.accent, language=config.language,
                show_thinking=config.show_thinking, close_to_tray=config.close_to_tray,
                log_stream=config.log_stream, proxy_url=config.proxy_url,
                llm_timeout_seconds=config.llm_timeout_seconds)
            page.network_group.hide()
            return page
        if key == "network":
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(16, 16, 16, 16)
            group = self.page("general").network_group
            layout.addWidget(group)
            group.show()
            layout.addStretch()
            return page
        if key == "instructions":
            page = InstructionsPage(config.prompts)
            sources = QPushButton(QCoreApplication.translate('SettingsDialog', "查看项目 AGENTS.md"))
            sources.setEnabled(bool(self.work_dir))
            sources.setToolTip(self.work_dir or QCoreApplication.translate('SettingsDialog', "当前未选择项目"))
            sources.clicked.connect(self.project_instructions_requested)
            page.layout().insertWidget(page.layout().count() - 1, sources)
            return page
        if key == "terminal":
            return TerminalPage(config.shell, shell_choices=self._shell_choices)
        if key == "capabilities":
            return CapabilitiesPage(config.prompts, providers=self._draft_providers(), capabilities=config.capabilities)
        if key == "modes":
            return ModesPage(self.work_dir, providers=self._draft_providers(), mode_catalog=self.mode_catalog_service,
                             modes=self._modes)
        if key == "about":
            return AboutPage()
        if key == "shortcuts":
            return ShortcutsPage(config.shortcuts)
        if key == "automation":
            page = AutomationPage()
            page.capture_requested.connect(self.capture_requested.emit)
            page.configure_requested.connect(self._configure_automation)
            page.shortcuts_requested.connect(lambda: self._select_page("shortcuts"))
            return page
        if key == "memory":
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(16, 16, 16, 16)
            layout.setSpacing(12)
            layout.addWidget(build_page_header(QCoreApplication.translate('SettingsDialog', "记忆与资料"), QCoreApplication.translate('SettingsDialog', "查看来源、管理记忆和阅读资料。")))
            library = FormSection(QCoreApplication.translate('SettingsDialog', "资料与上下文"))
            for title, detail, action, callback in (
                (QCoreApplication.translate('SettingsDialog', "记忆与资料"), QCoreApplication.translate('SettingsDialog', "阅读文件、审核项目记忆与用户偏好。"), QCoreApplication.translate('SettingsDialog', "打开"), self.library_requested.emit),
                (QCoreApplication.translate('SettingsDialog', "指令与来源"), QCoreApplication.translate('SettingsDialog', "管理全局指令和项目 AGENTS.md。"), QCoreApplication.translate('SettingsDialog', "查看"), lambda: self._select_page("instructions")),
                (QCoreApplication.translate('SettingsDialog', "压缩策略"), QCoreApplication.translate('SettingsDialog', "分别设置工具内容压缩与上下文压缩。"), QCoreApplication.translate('SettingsDialog', "设置"), lambda: self._select_page("strategy")),
            ):
                label = QLabel(f"{title}\n{detail}")
                label.setWordWrap(True)
                label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
                button = QPushButton(action)
                button.setMinimumWidth(64)
                button.setAccessibleName(f"{action} · {title}")
                button.clicked.connect(callback)
                library.form.addRow(label, button)
            layout.addWidget(library.group)
            layout.addStretch()
            return page
        raise KeyError(key)

    def _draft_providers(self):
        page = self._pages.get("models")
        return self.provider_catalog_service.snapshot(page.providers if page is not None else self.providers)

    def _draft_mcp_servers(self):
        page = self._pages.get("mcp")
        return page.installed.collect_servers() if page is not None else self._mcp_servers

    def _rebase_new_page(self, key: str) -> None:
        if not self._baseline_fingerprints:
            return
        fields = {
            "general": ("language", "theme", "accent", "show_thinking", "close_to_tray", "log_stream", "proxy_url", "llm_timeout_seconds"),
            "shortcuts": ("shortcuts",), "models": ("default_chat_model", "default_auxiliary_model"),
            "permissions": ("permissions",), "strategy": ("agent", "retry", "context"),
            "instructions": ("prompts",), "channels": ("channels",), "terminal": ("shell",), "ocr": ("ocr",),
        }.get(key, ())
        current = self._fingerprints(self.build_update())
        if domain := {"models": "providers", "mcp": "mcp", "modes": "modes", "search": "search"}.get(key):
            self._baseline_fingerprints[domain] = current[domain]
        baseline = json.loads(self._baseline_fingerprints["app_settings"])
        values = json.loads(current["app_settings"])
        for field in fields:
            baseline[field] = values[field]
        if key in {"capabilities", "ocr"}:
            # OCR and other capabilities have distinct editors but share one config.
            def owned(item):
                return (item["id"] == "ocr") == (key == "ocr")
            previous = baseline["capabilities"].get("capabilities", [])
            incoming = values["capabilities"].get("capabilities", [])
            baseline["capabilities"] = {**values["capabilities"], "capabilities":
                sorted([item for item in previous if not owned(item)] +
                       [item for item in incoming if owned(item)], key=lambda item: item["id"])}
        self._baseline_fingerprints["app_settings"] = json.dumps(baseline, ensure_ascii=False, sort_keys=True, default=str)

    def _init_pages(self) -> None:
        self.page_list.clear()
        self._page_specs = [
            SettingsPageSpec("general", QT_TRANSLATE_NOOP('SettingsDialog', "外观"), "general", keywords="appearance language english theme accent 外观 语言 英文 浅色 深色 主题 强调色 托盘 思考 日志"),
            SettingsPageSpec("shortcuts", QT_TRANSLATE_NOOP('SettingsDialog', "快捷键"), "general", keywords="keyboard hotkey 截图 快捷键"),
            SettingsPageSpec("models", QT_TRANSLATE_NOOP('SettingsDialog', "模型"), "models", False, 'provider api key endpoint 服务 连接 默认 辅助 密钥'),
            SettingsPageSpec("modes", QT_TRANSLATE_NOOP('SettingsDialog', "模式"), "runtime", False, 'agent mode 模型 工具 系统提示词'),
            SettingsPageSpec("permissions", QT_TRANSLATE_NOOP('SettingsDialog', "权限"), "runtime", False, 'approval sandbox allow deny 批准 确认 安全 工具 读写 执行'),
            SettingsPageSpec("strategy", QT_TRANSLATE_NOOP('SettingsDialog', "策略"), "runtime", keywords="context retry memory compression 上下文 压缩 重试 记忆 容量 并行 轮次"),
            SettingsPageSpec("instructions", QT_TRANSLATE_NOOP('SettingsDialog', "指令与来源"), "runtime", keywords="prompt agents.md instructions 全局 项目 提示词"),
            SettingsPageSpec("skills", QT_TRANSLATE_NOOP('SettingsDialog', "技能"), "tools", False, 'skills install market 安装 发现 市场 更新 导入 导出', False),
            SettingsPageSpec("mcp", "MCP", "tools", False, 'server stdio sse http tools 服务 连接 环境变量 安装 发现 市场 更新'),
            SettingsPageSpec("capabilities", QT_TRANSLATE_NOOP('SettingsDialog', "模型能力"), "tools", False, 'model tasks capability 翻译 总结 润色 提示词'),
            SettingsPageSpec("search", QT_TRANSLATE_NOOP('SettingsDialog', "搜索"), "tools", keywords="web search engine tavily searxng 网络 引擎 搜索"),
            SettingsPageSpec("automation", QT_TRANSLATE_NOOP('SettingsDialog', "电脑与浏览器"), "tools", keywords="computer browser capture 桌面 自动化 截图", edits_config=False),
            SettingsPageSpec("ocr", "OCR", "tools", keywords="pdf image recognition 图像 图片 文字识别 扫描"),
            SettingsPageSpec("memory", QT_TRANSLATE_NOOP('SettingsDialog', "记忆与资料"), "memory", keywords="library knowledge files 资料库 知识 文件", edits_config=False),
            SettingsPageSpec("channels", QT_TRANSLATE_NOOP('SettingsDialog', "消息通道"), "channels", False, 'telegram feishu discord gateway bot 飞书 渠道 机器人'),
            SettingsPageSpec("network", QT_TRANSLATE_NOOP('SettingsDialog', "网络与诊断"), "advanced", keywords="network proxy timeout diagnostics 代理 超时"),
            SettingsPageSpec("terminal", QT_TRANSLATE_NOOP('SettingsDialog', "终端"), "advanced", keywords="shell powershell bash command arguments 参数 命令 执行"),
            SettingsPageSpec("about", QT_TRANSLATE_NOOP('SettingsDialog', "关于"), "advanced", keywords="version update license github 版本 更新 许可证 开源", edits_config=False),
        ]
        self._nav_rows_by_page: dict[str, int] = {}
        self._page_tabs = {}
        self._page_hosts = {}
        self._search_index = {}
        for page_index, (group, title, icon) in enumerate(_GROUPS):
            source_title = title
            title = QCoreApplication.translate("SettingsDialog", title)
            specs = [spec for spec in self._page_specs if spec.group == group]
            tabs = None
            if len(specs) > 1:
                group_page = QWidget()
                group_layout = QVBoxLayout(group_page)
                group_layout.setContentsMargins(16, 16, 16, 8)
                group_layout.setSpacing(12)
                group_layout.addWidget(build_page_header(title))
                tabs = QTabWidget()
                tabs.setObjectName("settings_subtabs")
                group_layout.addWidget(tabs, 1)
                self.content.addWidget(group_page)
            for spec in specs:
                host = QWidget()
                QVBoxLayout(host).setContentsMargins(0, 0, 0, 0)
                self._page_hosts[spec.key] = host
                if tabs is None:
                    self.content.addWidget(host)
                else:
                    tab_title = QCoreApplication.translate("SettingsDialog", spec.title)
                    index = tabs.addTab(host, tab_title.replace("&", "&&"))
                    tabs.tabBar().setAccessibleTabName(index, tab_title)
                    self._page_tabs[spec.key] = (tabs, index)
                self._nav_rows_by_page[spec.key] = page_index
                self._search_index[spec.key] = " ".join((group, source_title, title, spec.key, spec.title,
                    QCoreApplication.translate("SettingsDialog", spec.title), spec.keywords)).casefold()
            if tabs is not None:
                tabs.currentChanged.connect(self._show_current_page)
            item = QListWidgetItem(Icons.get(icon), title)
            item.setData(PAGE_INDEX_ROLE, page_index)
            item.setData(PAGE_KEY_ROLE, specs[0].key)
            item.setData(NAV_GROUP_ROLE, group)
            self.page_list.addItem(item)
        self.search_empty = QLabel(QCoreApplication.translate('SettingsDialog', "没有匹配的设置\n试试页面名称或关键词"))
        self.search_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.search_empty.setProperty("muted", True)
        self.content.addWidget(self.search_empty)

    def _filter_pages(self, text: str):
        words = text.casefold().split()
        matches = [spec for spec in self._page_specs if all(word in self._search_index[spec.key] for word in words)]
        visible_keys = {spec.key for spec in matches}
        visible_rows = {self._nav_rows_by_page[spec.key] for spec in matches}
        self._navigating = True
        try:
            for spec in self._page_specs:
                if entry := self._page_tabs.get(spec.key):
                    entry[0].setTabVisible(entry[1], spec.key in visible_keys)
            for row in range(self.page_list.count()):
                self.page_list.item(row).setHidden(row not in visible_rows)
            if not matches:
                self.page_list.setCurrentRow(-1)
                self.content.setCurrentWidget(self.search_empty)
            elif self.page_list.currentRow() not in visible_rows:
                self.page_list.setCurrentRow(self._nav_rows_by_page[matches[0].key])
        finally:
            self._navigating = False
        self._show_current_page()

    def _sync_provider_dependent_pages(self) -> None:
        providers = self._draft_providers()
        for key in ("capabilities", "ocr", "modes"):
            if page := self._pages.get(key):
                page.set_providers(providers)

    def _wrap_page(self, page) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setObjectName("settings_page_scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(page)
        return scroll

    def _current_spec(self):
        for spec in self._page_specs:
            if self.page_list.currentRow() == self._nav_rows_by_page[spec.key]:
                entry = self._page_tabs.get(spec.key)
                if entry is None or entry[0].currentIndex() == entry[1]:
                    return spec
        return None

    def _show_current_page(self, *_args) -> None:
        if self._navigating:
            return
        if spec := self._current_spec():
            self.page(spec.key)
        self._update_save_footer()

    def _change_page(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current is not None:
            self.content.setCurrentIndex(int(current.data(PAGE_INDEX_ROLE)))
            self._show_current_page()

    def _select_page(self, page: str | QWidget) -> None:
        key = page if isinstance(page, str) else next(key for key, value in self._pages.items() if value is page)
        # Reset the filter without constructing an intermediate tab.
        self.search_input.blockSignals(True)
        self.search_input.clear()
        self.search_input.blockSignals(False)
        self._navigating = True
        try:
            for tabs, index in self._page_tabs.values():
                tabs.setTabVisible(index, True)
            for row in range(self.page_list.count()):
                self.page_list.item(row).setHidden(False)
            if key in self._page_tabs:
                tabs, index = self._page_tabs[key]
                tabs.setCurrentIndex(index)
            self.page_list.setCurrentRow(self._nav_rows_by_page[key])
        finally:
            self._navigating = False
        self._show_current_page()

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
                self._select_page(spec.key)
        provider_id = str(selected_provider_id or "").strip()
        if provider_id:
            self.page("models").select_provider(provider_id)

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

    def _collect_form_values_impl(self, *, prepare_channels: bool, show_errors: bool,
                                  only_page: str | None = None) -> bool:
        def active(key):
            return key in self._pages and (only_page is None or key == only_page)

        if active("modes"):
            modes = self._collect_section(
                QCoreApplication.translate('SettingsDialog', "模式配置无效"), self.page("modes"), self.page("modes").collect_modes, show_errors
            )
            if modes is _COLLECT_FAILED:
                return False
            self._modes = tuple(modes)

        if active("models"):
            # Draft comparison must not treat a stored, not-yet-usable connection as an edit.
            providers = self._collect_section(
                QCoreApplication.translate('SettingsDialog', "服务商配置无效"), self.page("models"),
                lambda: self.page("models").get_providers(validate_connection=show_errors), show_errors
            )
            if providers is _COLLECT_FAILED:
                return False
            self.providers = providers

            models_patch = self._collect_section(
                QCoreApplication.translate('SettingsDialog', "默认模型无效"),
                self.page("models"),
                lambda: {
                    "default_chat_model": self.page("models").collect_default_chat_model(),
                    "default_auxiliary_model": self.page("models").collect_default_auxiliary_model(),
                },
                show_errors,
            )
            if models_patch is _COLLECT_FAILED:
                return False
            self._settings_patch.update(models_patch)

        if active("mcp"):
            mcp_servers = self._collect_section(
                QCoreApplication.translate('SettingsDialog', "MCP 配置无效"), self.page("mcp"), self.page("mcp").installed.collect_servers, show_errors)
            if mcp_servers is _COLLECT_FAILED:
                if show_errors:
                    self.page("mcp").view_tabs.setCurrentIndex(0)
                    self.page("mcp").installed.editor.browser.show_detail()
                return False
            self._mcp_servers = tuple(mcp_servers)

        if active("search"):
            search_config = self._collect_section(
                QCoreApplication.translate('SettingsDialog', "搜索配置无效"), self.page("search"), self.page("search").collect, show_errors
            )
            if search_config is _COLLECT_FAILED:
                return False
            self.search_config = search_config

        if active("general"):
            appearance_patch = self._collect_section(
                QCoreApplication.translate('SettingsDialog', "通用配置无效"), self.page("general"), self.page("general").collect, show_errors
            )
            if appearance_patch is _COLLECT_FAILED:
                return False
            self._settings_patch.update(appearance_patch)

        if active("shortcuts"):
            shortcuts = self._collect_section(QCoreApplication.translate('SettingsDialog', "快捷键配置无效"), self.page("shortcuts"), self.page("shortcuts").collect, show_errors)
            if shortcuts is _COLLECT_FAILED:
                return False
            self._settings_patch["shortcuts"] = shortcuts

        if active("permissions"):
            perms = self._collect_section(
                QCoreApplication.translate('SettingsDialog', "权限配置无效"), self.page("permissions"), self.page("permissions").collect, show_errors
            )
            if perms is _COLLECT_FAILED:
                return False
            self._settings_patch["permissions"] = perms.to_dict()

        if active("strategy"):
            retry_config = self._collect_section(
                QCoreApplication.translate('SettingsDialog', "重试策略无效"), self.page("strategy"), self.page("strategy").collect_retry, show_errors
            )
            if retry_config is _COLLECT_FAILED:
                return False
            self._settings_patch["retry"] = retry_config.to_dict()

            agent_config = self._collect_section(
                QCoreApplication.translate('SettingsDialog', "Agent 策略无效"), self.page("strategy"), self.page("strategy").collect_agent, show_errors
            )
            if agent_config is _COLLECT_FAILED:
                return False
            self._settings_patch["agent"] = agent_config.to_dict()

            ctx = self._collect_section(
                QCoreApplication.translate('SettingsDialog', "压缩阈值无效"), self.page("strategy"), self.page("strategy").collect_context, show_errors
            )
            if ctx is _COLLECT_FAILED:
                return False
            self._settings_patch["context"] = ctx.to_dict()

        if active("instructions"):
            prompts = self._collect_section(
                QCoreApplication.translate('SettingsDialog', "指令配置无效"), self.page("instructions"), self.page("instructions").collect, show_errors
            )
            if prompts is _COLLECT_FAILED:
                return False
            self._settings_patch["prompts"] = prompts.to_dict()

        if active("capabilities"):
            capabilities = self._collect_section(
                QCoreApplication.translate('SettingsDialog', "能力配置无效"),
                self.page("capabilities"),
                self.page("capabilities").collect_capabilities,
                show_errors,
            )
            if capabilities is _COLLECT_FAILED:
                return False
            previous = CapabilitiesConfig.from_dict(self._settings_patch["capabilities"])
            self._settings_patch["capabilities"] = CapabilitiesConfig(capabilities=(
                *capabilities.capabilities, *(item for item in previous.capabilities if item.id == "ocr"))).to_dict()

        if active("channels"):
            try:
                channels = tuple(self.page("channels").collect())
                preferred_session_id = self.page("channels").get_preferred_session_id()
                if prepare_channels:
                    prepared_channels = self.channel_service.prepare_for_save(
                        channels,
                        preferred_session_id=preferred_session_id,
                    )
                    self._preferred_channel_session_id = prepared_channels.preferred_session_id
                    channels = prepared_channels.channels
                else:
                    self._preferred_channel_session_id = str(preferred_session_id or "").strip()
                self._settings_patch["channels"] = [channel.to_dict() for channel in channels]
            except Exception as exc:
                return self._validation_failure(QCoreApplication.translate('SettingsDialog', "频道配置无效"), exc, self.page("channels"), show_errors)

        if active("terminal"):
            terminal_patch = self._collect_section(
                QCoreApplication.translate('SettingsDialog', "终端配置无效"), self.page("terminal"), self.page("terminal").collect, show_errors
            )
            if terminal_patch is _COLLECT_FAILED:
                return False
            self._settings_patch.update(terminal_patch)

        if active("ocr"):
            ocr_config = self._collect_section(
                QCoreApplication.translate('SettingsDialog', "OCR 配置无效"), self.page("ocr"), self.page("ocr").collect, show_errors
            )
            if ocr_config is _COLLECT_FAILED:
                return False
            self._settings_patch["ocr"] = ocr_config.to_dict()

            capability = self._collect_section(QCoreApplication.translate('SettingsDialog', "OCR 模型配置无效"), self.page("ocr"), self.page("ocr").collect_capability, show_errors)
            if capability is _COLLECT_FAILED:
                return False
            previous = CapabilitiesConfig.from_dict(self._settings_patch["capabilities"])
            self._settings_patch["capabilities"] = CapabilitiesConfig(capabilities=(
                *(item for item in previous.capabilities if item.id != "ocr"), capability)).to_dict()

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
        box.setWindowTitle(QCoreApplication.translate('SettingsDialog', "未保存的设置"))
        box.setText(QCoreApplication.translate('SettingsDialog', "设置中有未保存的更改。"))
        box.setInformativeText(QCoreApplication.translate('SettingsDialog', "保存后关闭，或放弃这些配置草稿？"))
        box.setStandardButtons(
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel
        )
        save = box.button(QMessageBox.StandardButton.Save)
        discard = box.button(QMessageBox.StandardButton.Discard)
        cancel = box.button(QMessageBox.StandardButton.Cancel)
        if save is not None:
            save.setText(QCoreApplication.translate('SettingsDialog', "保存并关闭"))
        if discard is not None:
            discard.setText(QCoreApplication.translate('SettingsDialog', "放弃更改"))
        if cancel is not None:
            cancel.setText(QCoreApplication.translate('SettingsDialog', "继续编辑"))
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
        saved_domains = tuple(getattr(result, "saved_domains", ()) or ())
        snapshot = getattr(result, "snapshot", None)
        if snapshot is not None:
            if "app_settings" in saved_domains:
                self._app_config = AppConfig.from_dict(getattr(snapshot, "app_settings", {}) or {})
                values = self._app_config.to_dict()
                self._settings_patch = {key: values[key] for key in self._settings_patch}
            if "providers" in saved_domains:
                self.providers = list(getattr(snapshot, "providers", ()) or ())
            if "modes" in saved_domains:
                self._modes = tuple(getattr(snapshot, "modes", ()) or ())
            if "search" in saved_domains:
                self.search_config = getattr(snapshot, "search_config", self.search_config)
            if "mcp" in saved_domains and "mcp" not in self._pages:
                self._mcp_servers = tuple(snapshot.mcp_servers)
        # Opened forms project their current values; unopened fields adopt the
        # authoritative save result before successful domains are rebased.
        current = self._current_fingerprints() or dict(self._pending_fingerprints)
        for domain in saved_domains:
            if domain in current:
                self._baseline_fingerprints[domain] = current[domain]
            elif domain in self._pending_fingerprints:
                self._baseline_fingerprints[domain] = self._pending_fingerprints[domain]

        if "mcp" in saved_domains and "mcp" in self._pages:
            self.page("mcp").installed.mark_saved()
        self._dirty_hint = False
        self._refresh_dirty_state()
        failed_domains = tuple(getattr(result, "failed_domains", ()) or ())
        failed_stages = tuple(getattr(result, "failed_stages", ()) or ())
        if failed_domains:
            labels = [QCoreApplication.translate("SettingsDialog", self._DOMAIN_LABELS.get(domain, domain))
                      for domain in failed_domains]
            self._set_status(QCoreApplication.translate('SettingsDialog', "未保存：") + "、".join(labels), error=True)
        elif failed_stages:
            self._set_status(QCoreApplication.translate('SettingsDialog', "设置已保存，但运行时尚未完全应用。"), error=True)
        elif self._app_config.language != (QCoreApplication.instance().property("ui_language") or "zh_CN"):
            self._set_status(QCoreApplication.translate("SettingsDialog", "设置已保存。重启 PyCat 后切换界面语言。"))
        else:
            self._set_status(QCoreApplication.translate('SettingsDialog', "设置已保存。"))

        should_close = self._close_after_save and not failed_domains
        self._close_after_save = False
        self._pending_fingerprints = {}
        if should_close:
            self.close_without_prompt()

    def apply_save_error(self, error: Exception) -> None:
        self._set_saving(False)
        self._close_after_save = False
        self._set_status(QCoreApplication.translate('SettingsDialog', '保存失败：{error}').format(error=error), error=True)
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
        for key in ("mcp", "skills"):
            if page := self._pages.get(key):
                page.cancel_pending()
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
            self._set_status(QCoreApplication.translate('SettingsDialog', "正在保存设置…"), kind="saving")
        else:
            self._refresh_dirty_state()

    def _set_status(self, text: str, *, error: bool = False, kind: str = "") -> None:
        value = str(text or "").strip()
        self._status_kind = "error" if error else kind
        self.status_label.setText(value)
        self.status_label.setProperty("error", bool(error))
        self.status_label.setVisible(bool(value))
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self._update_save_footer()

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
        if now_dirty and not self.status_label.property("error"):
            self._set_status(QCoreApplication.translate('SettingsDialog', "有未保存的更改"), kind="dirty")
        elif not now_dirty and self._status_kind == "dirty":
            self._set_status("")
        self._update_save_footer()
        if was_dirty != now_dirty:
            self.dirty_changed.emit(now_dirty)
        self._dirty_hint = False

    def _update_save_footer(self):
        spec = self._current_spec()
        editable = spec is not None and spec.edits_config
        self.save_footer.setVisible(editable or bool(self._dirty_domain_cache) or self._saving or self._status_kind == "error")

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

        settings = dict(update.settings_patch or {})
        if capabilities := settings.get("capabilities"):
            # Capability identities, not the order in which editors were visited.
            settings["capabilities"] = {**capabilities, "capabilities":
                sorted(capabilities.get("capabilities", []), key=lambda item: item["id"])}
        payloads = {
            "providers": [payload(provider) for provider in update.providers],
            "app_settings": settings,
            "mcp": [payload(server) for server in update.mcp_servers],
            "modes": [payload(mode) for mode in update.modes],
            "search": payload(update.search_config) if update.search_config is not None else None,
        }
        return {
            domain: json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            for domain, value in payloads.items()
        }

    def _connect_dirty_tracking(self, page: QWidget) -> None:
        def callback(*_args):
            self.mark_dirty()

        root = page.installed if isinstance(page, ResourcePage) else page
        widgets = root.findChildren(QWidget)
        excluded = root.editor.search if isinstance(page, ResourcePage) else None
        def controls(widget_type):
            return (widget for widget in widgets if isinstance(widget, widget_type) and widget is not excluded)
        for widget in controls(QLineEdit):
            if widget.isReadOnly():
                continue
            widget.textChanged.connect(callback)
        for widget_type in (QTextEdit, QPlainTextEdit):
            for widget in controls(widget_type):
                if not widget.isReadOnly():
                    widget.textChanged.connect(callback)
        for widget in controls(QComboBox):
            widget.currentIndexChanged.connect(callback)
            if widget.isEditable():
                widget.currentTextChanged.connect(callback)
        for widget_type in (QSpinBox, QDoubleSpinBox):
            for widget in controls(widget_type):
                widget.valueChanged.connect(callback)
        for widget in controls(QAbstractButton):
            if widget.isCheckable():
                widget.toggled.connect(callback)

        seen_models: set[int] = set()
        for view in controls(QAbstractItemView):
            model = view.model()
            if model is None or id(model) in seen_models:
                continue
            seen_models.add(id(model))
            model.dataChanged.connect(callback)
            model.rowsInserted.connect(callback)
            model.rowsRemoved.connect(callback)
            model.modelReset.connect(callback)

    def build_update(self) -> AppSettingsUpdate:
        # Layout geometry has a separate owner; all settings drafts share this payload.
        settings = deepcopy(self._settings_patch)
        settings["show_sidebar"] = self._app_config.show_sidebar
        settings["show_stats"] = self._app_config.show_stats
        return AppSettingsUpdate(
            providers=tuple(self.providers), settings_patch=settings,
            mcp_servers=self._mcp_servers, modes=self._modes, search_config=self.search_config,
        )

    def get_preferred_channel_session_id(self) -> str:
        return str(self._preferred_channel_session_id or "").strip()
