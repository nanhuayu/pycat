"""Settings presenter - handles theme, proxy, and settings/provider UI flows."""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from PyQt6.QtWidgets import QApplication, QMessageBox

from core.app.services.mode_catalog import ModeCatalogService
from models.provider import Provider
from gui.settings.model_profile_dialog import ModelProfileDialog
from gui.settings.settings_dialog import SettingsDialog
from gui.about_content import PRODUCT_NAME, about_dialog_html
from gui.utils.theme import normalize_accent, palette_for_theme, render_theme_stylesheet

if TYPE_CHECKING:
    from gui.main_window import MainWindow

logger = logging.getLogger(__name__)


class SettingsPresenter:
    """Handles application settings, provider catalog, and shell-level settings UI."""

    def __init__(self, window: MainWindow):
        self._window = window
        self._applied_theme_key: str | None = None
        self._applied_stylesheet: str = ""

    def apply_theme(self) -> None:
        """Apply theme based on app settings."""
        try:
            project_root = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            theme = (self._window.app_settings.get('theme') or 'light').lower()
            accent = normalize_accent(self._window.app_settings.get("accent"))
            base_theme_path = os.path.join(project_root, 'assets', 'styles', 'base.qss')
            theme_path = os.path.join(project_root, 'assets', 'styles', 'theme.qss')

            parts: list[str] = []
            if os.path.exists(base_theme_path):
                with open(base_theme_path, 'r', encoding='utf-8') as f:
                    parts.append(f.read())
            if os.path.exists(theme_path):
                parts.append(render_theme_stylesheet(theme, theme_path, accent))

            if parts:
                stylesheet = "\n\n".join(parts)
                theme_key = f"{theme}:{accent}:{len(stylesheet)}"
                if theme_key != self._applied_theme_key or stylesheet != self._applied_stylesheet:
                    app = QApplication.instance()
                    if app is not None:
                        app.setProperty("theme", theme)
                        app.setProperty("accent", accent)
                        app.setPalette(palette_for_theme(theme, accent))
                        app.setStyleSheet(stylesheet)
                    self._window.setProperty("theme", theme)
                    self._window.setProperty("accent", accent)
                    self._sync_widget_theme(theme, accent)
                    self._window.setStyleSheet("")
                    self._applied_theme_key = theme_key
                    self._applied_stylesheet = stylesheet
        except Exception as e:
            logger.error("Error loading theme: %s", e)

    def _sync_widget_theme(self, theme: str, accent: str) -> None:
        for name in ("sidebar", "chat_view", "input_area", "inspector_panel"):
            widget = getattr(self._window, name, None)
            if widget is not None:
                widget.setProperty("theme", theme)
                widget.setProperty("accent", accent)
                widget.style().unpolish(widget)
                widget.style().polish(widget)
                widget.update()
        sidebar = getattr(self._window, "sidebar", None)
        conversation_list = getattr(sidebar, "conversation_list", None)
        if conversation_list is not None:
            conversation_list.setProperty("theme", theme)
            conversation_list.setProperty("accent", accent)
            conversation_list.viewport().update()

    def apply_proxy(self) -> None:
        """Update environment variables for HTTP proxy."""
        proxy = self._window.app_settings.get('proxy_url', '').strip()
        if proxy:
            os.environ['HTTP_PROXY'] = proxy
            os.environ['HTTPS_PROXY'] = proxy
        else:
            os.environ.pop('HTTP_PROXY', None)
            os.environ.pop('HTTPS_PROXY', None)

    def apply_provider_catalog(
        self,
        providers: list[Provider],
        *,
        selected_provider_id: str | None = None,
        selected_model: str | None = None,
        selected_model_ref: str | None = None,
        persist: bool = True,
    ) -> None:
        host = self._window
        next_providers = host.services.provider_catalog_service.snapshot(providers)
        if persist:
            if not host.services.provider_catalog_service.save(next_providers):
                raise RuntimeError("无法保存服务商配置")
        host.providers = next_providers
        try:
            host.inspector_panel.set_providers(host.providers)
        except Exception as e:
            logger.debug("Failed to sync providers into inspector panel: %s", e)
        host.input_area.set_providers(
            host.providers,
            selected_provider_id=selected_provider_id,
            selected_model=selected_model,
            selected_model_ref=selected_model_ref,
            emit_signal=False,
        )
        try:
            current_provider_id = host.input_area.get_selected_provider_id()
            current_model = host.input_area.get_selected_model()
            current_ref = str(selected_model_ref or "").strip() or host.services.app_coordinator.build_model_ref(
                providers=host.providers,
                provider_id=current_provider_id,
                model=current_model,
            )
            host.input_area.set_model_ref_options(host.providers, current_model_ref=current_ref)
        except Exception as e:
            logger.debug("Failed to refresh header model options after provider update: %s", e)
        host.services.app_coordinator.sync_catalog(providers=host.providers)
        if host.current_conversation:
            host.services.app_coordinator.remember_current_conversation(
                host.current_conversation,
                providers=host.providers,
                app_settings=host.app_settings,
                is_streaming=host.message_runtime.is_streaming(host.current_conversation.id),
            )
        else:
            host.window_state_presenter.sync_chat_header_from_input(
                provider_id=current_provider_id or None,
                model=current_model or None,
            )

    def open_provider_settings(self) -> None:
        host = self._window
        provider_id = host.input_area.get_selected_provider_id()
        self.open_settings(initial_page="models", selected_provider_id=provider_id)

    def edit_current_model(self) -> None:
        """Open the focused model editor and persist through the catalog service."""

        host = self._window
        provider_id = str(host.input_area.get_selected_provider_id() or "").strip()
        model_id = str(host.input_area.get_selected_model() or "").strip()
        provider = next(
            (
                item
                for item in host.providers
                if str(getattr(item, "id", "") or "").strip() == provider_id
            ),
            None,
        )
        if provider is None or not model_id:
            self.open_provider_settings()
            return

        profile = provider.find_model_profile(model_id) or provider.effective_model_profile(model_id)
        dialog = ModelProfileDialog(
            provider,
            model_id=model_id,
            profile=profile,
            parent=host,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return

        updated_provider = Provider.from_dict(provider.to_dict())
        updated_profile = dialog.accepted_profile()
        updated_provider.upsert_model(updated_profile)
        providers = host.services.provider_catalog_service.upsert(
            host.providers,
            updated_provider,
        )
        try:
            self.apply_provider_catalog(
                providers,
                selected_provider_id=updated_provider.id,
                selected_model=updated_profile.model_id,
            )
        except RuntimeError as exc:
            QMessageBox.warning(host, "模型保存失败", str(exc))

    def open_settings(self, *, initial_page: str = "", selected_provider_id: str = "") -> None:
        host = self._window
        work_dir = ""
        try:
            work_dir = str(getattr(host.current_conversation, "work_dir", "") or "") if host.current_conversation else ""
        except Exception as e:
            logger.debug("Failed to get work_dir for settings: %s", e)
            work_dir = ""

        dialog = SettingsDialog(
            host.providers,
            current_settings=host.app_settings,
            provider_service=host.services.provider_service,
            provider_catalog_service=host.services.provider_catalog_service,
            mode_catalog_service=getattr(host.services, "mode_catalog_service", None),
            repositories=host.services.repositories,
            channel_service=host.services.channel_service,
            tool_manager=host.services.tool_manager,
            parent=host,
            work_dir=work_dir,
            initial_page=initial_page,
            selected_provider_id=selected_provider_id,
        )
        if dialog.exec():
            update = dialog.build_update()
            selected_provider_id = host.input_area.get_selected_provider_id()
            selected_model = host.input_area.get_selected_model()
            default_chat_model = str(update.settings_patch.get("default_chat_model", "") or "").strip()
            preferred_channel_session_id = dialog.get_preferred_channel_session_id()
            next_app_settings = host.services.app_settings_service.apply_update(
                host.app_settings,
                update,
            )

            persistence_failures: list[str] = []
            if not host.services.provider_catalog_service.save(list(update.providers)):
                persistence_failures.append("服务商")
            if not host.services.app_settings_service.save(next_app_settings):
                persistence_failures.append("应用设置")
            if not host.services.repositories.mcp_servers.save(list(update.mcp_servers)):
                persistence_failures.append("MCP")
            mode_catalog_service = getattr(host.services, "mode_catalog_service", None) or ModeCatalogService()
            if not mode_catalog_service.save(list(update.modes)):
                persistence_failures.append("模式")
            if update.search_config is not None and not host.services.repositories.search_config.save(update.search_config):
                persistence_failures.append("搜索")
            if persistence_failures:
                QMessageBox.warning(
                    host,
                    "设置保存失败",
                    "以下配置未能保存：" + "、".join(persistence_failures),
                )
                return

            self.apply_provider_catalog(
                list(update.providers),
                selected_provider_id=selected_provider_id,
                selected_model=selected_model,
                selected_model_ref=default_chat_model if not host.current_conversation else "",
                persist=False,
            )
            host.app_settings = next_app_settings

            try:
                host.input_area.set_app_settings(host.app_settings)
                host.input_area.refresh_modes()
            except Exception as e:
                logger.debug("Failed to sync updated settings into input area: %s", e)

            self.apply_proxy()
            try:
                host.services.client.set_timeout(float(host.app_settings.get('llm_timeout_seconds', 600.0) or 600.0))
            except Exception as e:
                logger.debug("Failed to apply updated LLM timeout: %s", e)

            host.services.tool_manager.refresh_search_config()

            host.services.app_coordinator.remember_current_conversation(
                host.current_conversation,
                providers=host.providers,
                app_settings=host.app_settings,
                is_streaming=bool(
                    host.current_conversation
                    and host.message_runtime.is_streaming(host.current_conversation.id)
                ),
            )

            try:
                from core.config.app_settings import set_cached_settings
                set_cached_settings(host.app_settings)
            except Exception as e:
                logger.debug("Failed to refresh settings cache: %s", e)

            try:
                host.container.apply_runtime_configuration(host.app_settings)
            except Exception as e:
                logger.debug("Failed to apply runtime configuration: %s", e)

            try:
                host.services.channel_gateway.start(
                    host.services.channel_service.runtime_channels(host.app_settings)
                )
            except Exception as e:
                logger.debug("Failed to reload channel gateway after settings update: %s", e)

            try:
                conversations = host.services.conv_service.list_all()
                host.sidebar.update_conversations(conversations)
                host.services.app_coordinator.sync_catalog(
                    providers=host.providers,
                    conversation_count=len(conversations),
                )
                current_id = str(getattr(host.current_conversation, "id", "") or "").strip()
                focus_id = str(preferred_channel_session_id or current_id).strip()
                if focus_id:
                    host.sidebar.select_conversation(focus_id)
                    if str(preferred_channel_session_id or "").strip():
                        host.conversation_presenter.select(focus_id)
            except Exception as e:
                logger.debug("Failed to refresh sidebar after settings update: %s", e)

            self.apply_shell_visibility(
                show_sidebar=bool(host.app_settings.get("show_sidebar", True)),
                show_stats=bool(host.app_settings.get("show_stats", False)),
            )
            try:
                host.tray_controller.set_enabled(bool(host.app_settings.get("close_to_tray", True)))
            except Exception as e:
                logger.debug("Failed to apply tray setting: %s", e)
            self.apply_theme()

    def toggle_sidebar_panel(self, visible: bool) -> None:
        host = self._window
        host.app_settings['show_sidebar'] = bool(visible)
        self.apply_shell_visibility(show_sidebar=bool(visible))
        host.services.app_settings_service.save(host.app_settings)

    def toggle_inspector_panel(self, visible: bool) -> None:
        host = self._window
        host.app_settings['show_stats'] = bool(visible)
        self.apply_shell_visibility(show_stats=bool(visible))
        host.services.app_settings_service.save(host.app_settings)

    def apply_shell_visibility(
        self,
        *,
        show_sidebar: bool | None = None,
        show_stats: bool | None = None,
    ) -> None:
        host = self._window
        if show_sidebar is None:
            show_sidebar = bool(host.app_settings.get("show_sidebar", True))
        if show_stats is None:
            show_stats = bool(host.app_settings.get("show_stats", False))

        host.sidebar.setVisible(bool(show_sidebar))
        host.inspector_panel.setVisible(bool(show_stats))
        self._sync_visibility_controls(
            show_sidebar=bool(show_sidebar),
            show_stats=bool(show_stats),
        )

    def _sync_visibility_controls(self, *, show_sidebar: bool, show_stats: bool) -> None:
        host = self._window
        for name, value in (
            ("toggle_sidebar_action", show_sidebar),
            ("toggle_sidebar_btn", show_sidebar),
            ("toggle_inspector_action", show_stats),
            ("toggle_inspector_btn", show_stats),
        ):
            control = getattr(host, name, None)
            if control is None:
                continue
            try:
                control.blockSignals(True)
                control.setChecked(bool(value))
            finally:
                try:
                    control.blockSignals(False)
                except Exception:
                    pass

    def persist_main_splitter_layout(self, _pos: int, _index: int) -> None:
        self._persist_splitter_layout('splitter_sizes', self._window.splitter.sizes(), 'splitter')

    def persist_chat_splitter_layout(self, _pos: int, _index: int) -> None:
        self._persist_splitter_layout('chat_splitter_sizes', self._window.chat_splitter.sizes(), 'chat splitter')

    def reset_default_layout(self) -> None:
        host = self._window
        for key in ("main_window_size", "splitter_sizes", "chat_splitter_sizes"):
            host.app_settings.pop(key, None)
        host.apply_window_size()
        host.splitter.setSizes([180, 700, 200])
        host.chat_splitter.setSizes([520, 140])
        try:
            host.services.app_settings_service.save(host.app_settings)
        except Exception as exc:
            logger.debug("Failed to persist default window layout: %s", exc)
    
    def apply_bootstrap_shell_state(
        self,
        *,
        show_sidebar: bool,
        show_stats: bool,
        splitter_sizes: list[int] | None,
        chat_splitter_sizes: list[int] | None,
    ) -> None:
        host = self._window
        host.app_settings["show_sidebar"] = bool(show_sidebar)
        host.app_settings["show_stats"] = bool(show_stats)
        self.apply_shell_visibility(show_sidebar=bool(show_sidebar), show_stats=bool(show_stats))

        if splitter_sizes is not None:
            try:
                host.splitter.setSizes(list(splitter_sizes))
            except Exception as e:
                logger.debug("Failed to restore splitter sizes: %s", e)

        if chat_splitter_sizes is not None:
            try:
                host.chat_splitter.setSizes(list(chat_splitter_sizes))
            except Exception as e:
                logger.debug("Failed to restore chat splitter sizes: %s", e)

    def show_about(self) -> None:
        QMessageBox.about(
            self._window,
            f"关于 {PRODUCT_NAME}",
            about_dialog_html(),
        )

    def _persist_splitter_layout(self, key: str, sizes: list[int], label: str) -> None:
        host = self._window
        try:
            host.app_settings[key] = [int(x) for x in sizes]
            host.services.app_settings_service.save(host.app_settings)
        except Exception as e:
            logger.debug("Failed to persist %s layout: %s", label, e)
