"""Settings presenter - handles theme, proxy, and settings/provider UI flows."""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from PyQt6.QtCore import QThreadPool
from PyQt6.QtWidgets import QApplication, QMessageBox

from gui.about_content import PRODUCT_NAME, about_dialog_html
from gui.runtime.background_job import BackgroundJob
from gui.settings.model_profile_dialog import ModelProfileDialog
from gui.settings.settings_dialog import SettingsDialog
from gui.utils.theme import normalize_accent, palette_for_theme, render_theme_stylesheet
from models.model_ref import split_model_ref
from models.provider import Provider

if TYPE_CHECKING:
    from gui.main_window import MainWindow

logger = logging.getLogger(__name__)


class SettingsPresenter:
    """Handles application settings, provider catalog, and shell-level settings UI."""

    def __init__(self, window: MainWindow):
        self._window = window
        self._applied_theme_key: str | None = None
        self._applied_stylesheet: str = ""
        self._settings_dialog: SettingsDialog | None = None
        self._settings_job: BackgroundJob | None = None

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
                refresh_theme = getattr(widget, "refresh_theme", None)
                if callable(refresh_theme):
                    refresh_theme()
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
        host.input_area.set_providers(
            host.providers,
            selected_provider_id=selected_provider_id,
            selected_model=selected_model,
            selected_model_ref=selected_model_ref,
            emit_signal=False,
        )
        current_provider_id = host.input_area.get_selected_provider_id()
        current_model = host.input_area.get_selected_model()
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

    def edit_current_model(self) -> bool:
        """Edit the model selected by the Composer."""

        host = self._window
        return self.edit_model_profile(
            provider_id=host.input_area.get_selected_provider_id(),
            model_id=host.input_area.get_selected_model(),
        )

    def edit_model_ref(self, model_ref: str) -> bool:
        """Edit a ``provider|model`` reference from another GUI surface."""

        host = self._window
        provider_name, model_id = split_model_ref(model_ref)
        provider = host.services.conv_service.resolve_provider(
            host.providers,
            provider_name=provider_name,
        )
        return self.edit_model_profile(
            provider_id=str(getattr(provider, "id", "") or ""),
            model_id=model_id,
        )

    def edit_model_profile(self, *, provider_id: str, model_id: str) -> bool:
        """Open the shared profile editor and persist through the catalog service."""

        host = self._window
        raise_approval = getattr(getattr(host, "message_presenter", None), "raise_pending_approval", None)
        if callable(raise_approval) and raise_approval():
            return False
        provider, _index = host.services.provider_catalog_service.find(
            host.providers,
            str(provider_id or "").strip(),
        )
        model_id = str(model_id or "").strip()
        if provider is None or not model_id:
            QMessageBox.information(host, "无法编辑模型", "请先选择一个已配置的模型。")
            return False

        profile = provider.find_model_profile(model_id) or provider.effective_model_profile(model_id)
        dialog = ModelProfileDialog(
            provider,
            model_id=model_id,
            profile=profile,
            parent=host,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return False

        try:
            providers = host.services.provider_catalog_service.save_model_profile(
                provider.id,
                dialog.accepted_profile(),
            )
            self.apply_provider_catalog(
                providers,
                selected_provider_id=host.input_area.get_selected_provider_id(),
                selected_model=host.input_area.get_selected_model(),
                persist=False,
            )
        except Exception as exc:
            QMessageBox.warning(host, "模型保存失败", str(exc))
            return False
        return True

    def open_settings(self, *, initial_page: str = "", selected_provider_id: str = "") -> None:
        host = self._window
        raise_approval = getattr(getattr(host, "message_presenter", None), "raise_pending_approval", None)
        if callable(raise_approval) and raise_approval():
            return
        if self._settings_dialog is not None:
            self._settings_dialog.focus_page(
                initial_page,
                selected_provider_id=selected_provider_id,
            )
            return
        work_dir = ""
        try:
            work_dir = str(getattr(host.current_conversation, "work_dir", "") or "") if host.current_conversation else ""
        except Exception as e:
            logger.debug("Failed to get work_dir for settings: %s", e)
            work_dir = ""

        settings_service = host.services.settings_update_service
        snapshot = settings_service.load_snapshot()

        def reload_mcp_servers():
            return settings_service.load_snapshot().mcp_servers

        dialog = SettingsDialog(
            list(snapshot.providers),
            current_settings=host.app_settings,
            provider_service=host.services.provider_service,
            provider_catalog_service=host.services.provider_catalog_service,
            mode_catalog_service=getattr(host.services, "mode_catalog_service", None),
            mcp_servers=snapshot.mcp_servers,
            search_config=snapshot.search_config,
            mcp_server_provider=reload_mcp_servers,
            channel_service=host.services.channel_service,
            tool_manager=host.services.tool_manager,
            skill_service=getattr(host.services, "skill_service", None),
            parent=host,
            work_dir=work_dir,
            initial_page=initial_page,
            selected_provider_id=selected_provider_id,
        )
        self._settings_dialog = dialog
        about_page = getattr(dialog, "about_page", None)
        if about_page is not None:
            check_requested = getattr(about_page, "check_requested", None)
            if check_requested is not None:
                check_requested.connect(host.check_for_updates)
            release_open_requested = getattr(about_page, "release_open_requested", None)
            if release_open_requested is not None:
                release_open_requested.connect(host.open_release_url)
            release_ignore_requested = getattr(about_page, "release_ignore_requested", None)
            if release_ignore_requested is not None:
                release_ignore_requested.connect(host.ignore_release)
            self.set_release_checking(bool(getattr(host, "_release_job", None)))
            self.apply_release_result(getattr(host, "_release_result", None))
        dialog.save_requested.connect(
            lambda update, dialog=dialog: self._start_settings_save(dialog, update)
        )
        dialog.finished.connect(lambda _result, dialog=dialog: self._release_settings_dialog(dialog))
        dialog.open()
        dialog.raise_()
        dialog.activateWindow()

    def _release_settings_dialog(self, dialog: SettingsDialog) -> None:
        if self._settings_dialog is dialog and self._settings_job is not None:
            self._settings_job.abandon()
            self._settings_job = None
        if self._settings_dialog is dialog:
            self._settings_dialog = None
        dialog.deleteLater()

    def _start_settings_save(self, dialog: SettingsDialog, update) -> None:
        if dialog is not self._settings_dialog or self._settings_job is not None:
            return
        host = self._window
        current_settings = dict(host.app_settings or {})
        current_providers = tuple(host.providers or ())

        def operation():
            return host.services.settings_update_service.apply(
                update,
                current_settings=current_settings,
                current_providers=current_providers,
            )

        job = BackgroundJob(operation)
        self._settings_job = job
        job.signals.finished.connect(
            lambda result, error, dialog=dialog, update=update, job=job: self._finish_settings_save(
                dialog,
                update,
                job,
                result,
                error,
            )
        )
        QThreadPool.globalInstance().start(job)

    def _finish_settings_save(
        self,
        dialog: SettingsDialog,
        update,
        job: BackgroundJob,
        result,
        error,
    ) -> None:
        if self._settings_job is not job:
            return
        self._settings_job = None
        if dialog is not self._settings_dialog:
            return
        if error is not None:
            dialog.apply_save_error(error)
            QMessageBox.warning(dialog, "设置保存失败", str(error))
            return
        self._apply_settings_result(dialog, update, result)

    _SETTINGS_DOMAIN_LABELS = {
        "providers": "服务商",
        "app_settings": "应用设置",
        "mcp": "MCP",
        "modes": "模式",
        "search": "搜索",
    }
    _SETTINGS_STAGE_LABELS = {
        "validate": "配置校验",
        "runtime": "运行时刷新",
        "channel": "Channel 协调",
    }

    def _apply_settings_dialog(self, dialog: SettingsDialog) -> None:
        """Synchronous compatibility entry used by focused tests and callers."""

        host = self._window
        update = dialog.build_update()
        result = host.services.settings_update_service.apply(
            update,
            current_settings=host.app_settings,
            current_providers=host.providers,
        )
        self._apply_settings_result(dialog, update, result)

    def _apply_settings_result(self, dialog: SettingsDialog, update, result) -> None:
        host = self._window
        selected_provider_id = host.input_area.get_selected_provider_id()
        selected_model = host.input_area.get_selected_model()
        default_chat_model = str(update.settings_patch.get("default_chat_model", "") or "").strip()
        preferred_channel_session_id = dialog.get_preferred_channel_session_id()
        host.app_settings = result.app_settings

        self.apply_provider_catalog(
            list(result.snapshot.providers),
            selected_provider_id=selected_provider_id,
            selected_model=selected_model,
            selected_model_ref=(
                str(result.app_settings.get("default_chat_model", default_chat_model) or "").strip()
                if not host.current_conversation
                else ""
            ),
            persist=False,
        )

        try:
            host.input_area.set_app_settings(host.app_settings)
            host.input_area.refresh_modes()
        except Exception as e:
            logger.debug("Failed to sync updated settings into input area: %s", e)

        self.apply_proxy()

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
            conversations = host.services.conv_service.list_all()
            host.sidebar.update_conversations(conversations)
            host.services.app_coordinator.sync_catalog(
                providers=host.providers,
                conversation_count=len(conversations),
            )
            current_id = str(getattr(host.current_conversation, "id", "") or "").strip()
            saved_channel_settings = "app_settings" in result.saved_domains
            preferred_id = preferred_channel_session_id if saved_channel_settings else ""
            focus_id = str(preferred_id or current_id).strip()
            if focus_id:
                host.sidebar.select_conversation(focus_id)
                if str(preferred_id or "").strip():
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

        if not result.ok:
            QMessageBox.warning(
                dialog,
                "设置未完全应用",
                self._format_settings_failure(result),
            )
        apply_result = getattr(dialog, "apply_save_result", None)
        if callable(apply_result):
            apply_result(result)

    def _format_settings_failure(self, result) -> str:
        lines: list[str] = []
        if result.saved_domains:
            labels = [
                self._SETTINGS_DOMAIN_LABELS.get(domain, domain)
                for domain in result.saved_domains
            ]
            lines.append("已保存：" + "、".join(labels))
        if result.failed_domains:
            labels = [
                self._SETTINGS_DOMAIN_LABELS.get(domain, domain)
                for domain in result.failed_domains
            ]
            lines.append("未保存：" + "、".join(labels))
        for stage, error in result.failed_stages:
            label = self._SETTINGS_STAGE_LABELS.get(stage, stage)
            detail = str(error or "未知错误").strip()
            lines.append(f"{label}失败：{detail}")
        return "\n".join(lines) or "设置更新失败。"

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
        host.splitter.setSizes([180, 660, 240])
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

    def set_release_checking(self, checking: bool) -> None:
        dialog = self._settings_dialog
        page = getattr(dialog, "about_page", None) if dialog is not None else None
        setter = getattr(page, "set_release_checking", None)
        if callable(setter):
            setter(bool(checking))

    def apply_release_result(self, result, *, ignored_tag: str = "") -> None:
        dialog = self._settings_dialog
        page = getattr(dialog, "about_page", None) if dialog is not None else None
        setter = getattr(page, "set_release_result", None)
        if callable(setter):
            setter(result, ignored_tag=ignored_tag)

    def _persist_splitter_layout(self, key: str, sizes: list[int], label: str) -> None:
        host = self._window
        try:
            host.app_settings[key] = [int(x) for x in sizes]
            host.services.app_settings_service.save(host.app_settings)
        except Exception as e:
            logger.debug("Failed to persist %s layout: %s", label, e)
