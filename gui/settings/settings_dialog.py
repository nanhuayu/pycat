"""Settings dialog (thin container).

This dialog hosts application-wide pages under ``gui.settings.pages``.
"""

from __future__ import annotations

import logging
import json
from collections.abc import Callable, Iterable
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
)

from core.app import AppSettingsUpdate
from core.app.services.channel import ChannelService
from core.tools.manager import ToolManager
from models.provider import Provider
from core.app.services.provider_catalog import ProviderCatalogService
from core.app.services.provider import ProviderService
from core.app.services.mode_catalog import ModeCatalogService
from core.app.services.skill import SkillService
from models.contracts.config import AppConfig
from models.contracts.mcp import McpServerConfig
from models.search_config import SearchConfig

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
    save_requested = pyqtSignal(object)
    dirty_changed = pyqtSignal(bool)

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
        mcp_servers: Iterable[McpServerConfig] = (),
        search_config: SearchConfig | None = None,
        mcp_server_provider: Callable[[], Iterable[McpServerConfig]] | None = None,
        channel_service: ChannelService | None = None,
        tool_manager: ToolManager | None = None,
        skill_service: SkillService | None = None,
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
        if provider_catalog_service is None:
            raise ValueError("SettingsDialog requires ProviderCatalogService")
        self.provider_catalog_service = provider_catalog_service
        if channel_service is None:
            raise ValueError("SettingsDialog requires ChannelService")
        self.channel_service = channel_service
        self.tool_manager = tool_manager
        self.skill_service = skill_service or SkillService()
        self.providers = self.provider_catalog_service.snapshot(self.providers)
        self.search_config = search_config or SearchConfig()
        self._mcp_servers = tuple(mcp_servers or ())
        self._mcp_server_provider = mcp_server_provider
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

        self._setup_ui()
        self._connect_dirty_tracking()
        initial = self._current_fingerprints()
        self._baseline_fingerprints = dict(initial or {})
        self._tracking_ready = True
        self._refresh_dirty_state()

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
        self.status_label = QLabel("")
        self.status_label.setObjectName("settings_save_status")
        self.status_label.setProperty("muted", True)
        self.status_label.setWordWrap(True)
        self.status_label.setVisible(False)
        sidebar_layout.addWidget(self.status_label)

        self.save_btn = QPushButton("保存")
        self.save_btn.setObjectName("settings_action_btn")
        self.save_btn.setProperty("primary", True)
        self.save_btn.setIcon(Icons.get(Icons.SAVE, color=theme_tokens("light").color("on_primary")))
        self.save_btn.clicked.connect(lambda _checked=False: self.request_save())
        sidebar_layout.addWidget(self.save_btn)

        self.close_btn = QPushButton("关闭")
        self.close_btn.setObjectName("settings_action_btn")
        self.close_btn.clicked.connect(lambda _checked=False: self.request_close())
        sidebar_layout.addWidget(self.close_btn)

        self._save_shortcut = QShortcut(QKeySequence.StandardKey.Save, self)
        self._save_shortcut.activated.connect(self.request_save)

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
        self.mcp_page = McpPage(
            servers=self._mcp_servers,
            reload_provider=self._mcp_server_provider,
        )
        self.skills_page = SkillsPage(
            work_dir=self.work_dir,
            skill_service=self.skill_service,
        )
        self.general_page = GeneralPage(
            theme=self._app_config.theme,
            accent=self._app_config.accent,
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
        try:
            self._modes = tuple(self.modes_page.collect_modes())
        except Exception as exc:
            return self._validation_failure("模式配置无效", exc, self.modes_page, show_errors)

        try:
            self._mcp_servers = tuple(self.mcp_page.collect_servers())
        except Exception as exc:
            return self._validation_failure("MCP 配置无效", exc, self.mcp_page, show_errors)

        try:
            self.providers = self.models_page.get_providers()
        except Exception as exc:
            return self._validation_failure("服务商配置无效", exc, self.models_page, show_errors)

        try:
            self._models_patch = {
                "default_chat_model": self.models_page.collect_default_chat_model(),
                "default_auxiliary_model": self.models_page.collect_default_auxiliary_model(),
            }
        except Exception as exc:
            return self._validation_failure("默认模型无效", exc, self.models_page, show_errors)

        try:
            self.search_config = self.search_page.collect()
        except Exception as exc:
            return self._validation_failure("搜索配置无效", exc, self.general_page, show_errors)

        try:
            self._appearance_patch = dict(self.appearance_page.collect() or {})
            self._general_patch = dict(self._appearance_patch)
        except Exception as exc:
            return self._validation_failure("通用配置无效", exc, self.general_page, show_errors)

        try:
            perms = self.permissions_page.collect()
            self._permissions_patch = {"permissions": perms.to_dict()}
        except Exception as exc:
            return self._validation_failure("权限配置无效", exc, self.permissions_page, show_errors)

        try:
            self._retry_patch = self.strategy_page.collect_retry().to_dict()
        except Exception as exc:
            return self._validation_failure("重试策略无效", exc, self.strategy_page, show_errors)

        try:
            self._agent_patch = self.strategy_page.collect_agent().to_dict()
        except Exception as exc:
            return self._validation_failure("Agent 策略无效", exc, self.strategy_page, show_errors)

        try:
            ctx = self.strategy_page.collect_context()
            self._context_patch = {"context": ctx.to_dict()}
        except Exception as exc:
            return self._validation_failure("压缩阈值无效", exc, self.strategy_page, show_errors)

        try:
            prompts = self.instructions_page.collect()
            self._capability_patch = {
                "prompts": prompts.to_dict(),
                "capabilities": self.capabilities_page.collect_capabilities().to_dict(),
            }
        except Exception as exc:
            return self._validation_failure("能力配置无效", exc, self.capabilities_page, show_errors)

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

        try:
            self._terminal_patch = dict(self.terminal_page.collect() or {})
        except Exception as exc:
            return self._validation_failure("终端配置无效", exc, self.general_page, show_errors)

        return True

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
        self._allow_close = True
        super().reject()

    def closeEvent(self, event) -> None:
        if self._allow_close:
            event.accept()
            return
        if self._saving:
            event.ignore()
            return
        if not self.is_dirty():
            event.accept()
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
            if "mcp" in saved_domains:
                self._mcp_servers = tuple(getattr(snapshot, "mcp_servers", ()) or ())
            if "modes" in saved_domains:
                self._modes = tuple(getattr(snapshot, "modes", ()) or ())
            if "search" in saved_domains:
                self.search_config = getattr(snapshot, "search_config", self.search_config)
        if "mcp" in saved_domains:
            try:
                self.mcp_page.mark_saved()
            except Exception as exc:
                logger.debug("Failed to rebase MCP draft after save: %s", exc)
        for domain in saved_domains:
            if domain in current:
                self._baseline_fingerprints[domain] = current[domain]
            elif domain in self._pending_fingerprints:
                self._baseline_fingerprints[domain] = self._pending_fingerprints[domain]

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
            QTimer.singleShot(0, self.close_without_prompt)

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
        QTimer.singleShot(0, self._refresh_dirty_state)

    def _set_saving(self, saving: bool) -> None:
        self._saving = bool(saving)
        self.page_list.setEnabled(not self._saving)
        self.content.setEnabled(not self._saving)
        self.close_btn.setEnabled(not self._saving)
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
            self._set_status("有未保存的更改")
        elif not now_dirty and self.status_label.text() == "有未保存的更改":
            self._set_status("")
        if was_dirty != now_dirty:
            self.dirty_changed.emit(now_dirty)
        self._dirty_hint = False

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
