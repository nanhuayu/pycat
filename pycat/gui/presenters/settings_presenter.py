"""Settings presenter - handles theme, proxy, and settings/provider UI flows."""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtCore import QCoreApplication, QObject, QThreadPool, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QApplication, QMessageBox

from pycat.core.agent.policy import RunPolicyBuilder
from pycat.core.app.services.release import (
    STABLE_RELEASE_PAGE,
    ReleaseCheckResult,
    update_check_due,
)
from pycat.core.version import __version__
from pycat.gui.about_content import PRODUCT_NAME, about_dialog_html
from pycat.gui.runtime.background_job import BackgroundJob
from pycat.gui.settings.model_profile_dialog import ModelProfileDialog
from pycat.gui.settings.settings_dialog import SettingsDialog
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.theme import normalize_accent, palette_for_theme, render_theme_stylesheet
from pycat.models.model_ref import split_model_ref
from pycat.models.provider import Provider
from pycat.models.workspace import WorkspaceLocation

if TYPE_CHECKING:
    from pycat.gui.main_window import MainWindow

logger = logging.getLogger(__name__)


class SettingsPresenter:
    """Handles application settings, provider catalog, and shell-level settings UI."""

    def __init__(self, window: MainWindow):
        self._window = window
        self._applied_theme_key: str | None = None
        self._applied_stylesheet: str = ""
        self._settings_dialog: SettingsDialog | None = None
        self._close_window_after_save = False
        self._settings_job: BackgroundJob | None = None
        self._release_job: BackgroundJob | None = None
        self._release_result: ReleaseCheckResult | None = None
        if isinstance(window, QObject):
            window.destroyed.connect(self.abandon_release_job)

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
        for name in ("toggle_sidebar_btn", "toggle_inspector_btn", "more_btn", "update_available_btn"):
            button = getattr(self._window, name, None)
            if button is not None:
                color = Icons.COLOR_PRIMARY if name == "update_available_btn" else Icons.COLOR_MUTED
                button.setIcon(Icons.get(button.property("icon_name"), color=color, scale_factor=0.9))
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
        raise_approval = getattr(getattr(host, "interaction_presenter", None), "raise_pending_approval", None)
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
        raise_approval = getattr(getattr(host, "interaction_presenter", None), "raise_pending_approval", None)
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

        evaluation_conversation = host.current_conversation
        evaluation_provider = next((p for p in host.providers if evaluation_conversation is not None and p.id == evaluation_conversation.provider_id), None)
        evaluation_policy = RunPolicyBuilder.build(conversation=evaluation_conversation, app_settings=host.app_settings) if evaluation_conversation else None
        async def evaluate_candidate(candidate, suite, cancel_event):
            if evaluation_provider is None or evaluation_conversation is None:
                raise ValueError("请先为会话选择可用的模型。")
            return await host.services.skill_service.evaluate_candidate(candidate["id"], work_dir=work_dir,
                scope=candidate["scope"], suite=suite, runtime=host.services.agent_runtime, provider=evaluation_provider,
                model=evaluation_conversation.model, parent_policy=evaluation_policy, app_settings=host.app_settings,
                cancel_event=cancel_event)

        dialog = SettingsDialog(
            list(snapshot.providers),
            current_settings=snapshot.app_settings,
            provider_service=host.services.provider_service,
            provider_catalog_service=host.services.provider_catalog_service,
            mode_catalog_service=getattr(host.services, "mode_catalog_service", None),
            mcp_servers=snapshot.mcp_servers,
            modes=snapshot.modes,
            search_config=snapshot.search_config,
            mcp_server_provider=reload_mcp_servers,
            channel_service=host.services.channel_service,
            tool_manager=host.services.tool_manager,
            shell_choices=host.services.tools.shell_choices(),
            skill_service=getattr(host.services, "skill_service", None),
            extension_service=getattr(host.services, "extension_service", None),
            candidate_evaluator=evaluate_candidate,
            parent=host,
            work_dir=work_dir,
            initial_page=initial_page,
            selected_provider_id=selected_provider_id,
            embedded=True,
        )
        self._settings_dialog = dialog
        capture = getattr(self._window, 'screenshot_controller', None)
        if capture is not None:
            dialog.capture_requested.connect(capture.start)
        dialog.page_created.connect(self._configure_settings_page)
        for key, page in dialog.created_pages.items():
            self._configure_settings_page(key, page)
        dialog.library_requested.connect(lambda: self._leave_settings_for_library(dialog))
        dialog.project_instructions_requested.connect(lambda: self._leave_settings_for_library(dialog, work_dir=work_dir))
        dialog.save_requested.connect(
            lambda update, dialog=dialog: self._start_settings_save(dialog, update)
        )
        dialog.finished.connect(lambda _result, dialog=dialog: self._release_settings_dialog(dialog))
        host.workspace_stack.addWidget(dialog)
        host.workspace_stack.setCurrentWidget(dialog)
        dialog.show()
        host.window_state_presenter.refresh_menu_action_states()

    def _configure_settings_page(self, key: str, page) -> None:
        """Connect a page when first created; never materialize hidden views."""
        capture = getattr(self._window, "screenshot_controller", None)
        if key == "automation":
            page.preview_button.setEnabled(capture is not None)
            if capture is not None:
                capture.failed.connect(page.notice.setText)
        elif key == "shortcuts" and capture is not None:
            page.set_capture_status(capture.shortcut_status)
            capture.shortcut_changed.connect(page.set_capture_status)
        elif key == "about":
            page.check_requested.connect(self.check_for_updates)
            page.release_open_requested.connect(self.open_release_url)
            page.release_ignore_requested.connect(self.ignore_release)
            self.set_release_checking(self._release_job is not None)
            self.apply_release_result(self._release_result)

    def allow_window_close(self) -> bool:
        """Retain settings drafts until the existing close/save transaction finishes."""
        dialog = self._settings_dialog
        if dialog is None:
            return True
        dialog.request_close()
        if self._settings_dialog is None:
            return True
        self._close_window_after_save = dialog._close_after_save
        return False

    def _leave_settings_for_library(self, dialog, *, work_dir=""):
        def navigate(_result):
            def open_target():
                if work_dir:
                    if WorkspaceLocation.parse(work_dir).is_remote:
                        self._window.knowledge_presenter.open_content(workspace_path="AGENTS.md")
                    else:
                        self._window.knowledge_presenter.open_content(path=Path(work_dir) / "AGENTS.md")
                else:
                    self._window.knowledge_presenter.show_library()
            QTimer.singleShot(0, open_target)
        dialog.finished.connect(navigate)
        dialog.reject()
        if dialog.isVisible() and not dialog._close_after_save:
            dialog.finished.disconnect(navigate)

    def _release_settings_dialog(self, dialog: SettingsDialog) -> None:
        if self._settings_dialog is dialog and self._settings_job is not None:
            self._settings_job.abandon()
            self._settings_job = None
        if self._settings_dialog is dialog:
            self._settings_dialog = None
            self._window.workspace_stack.setCurrentWidget(self._window.splitter)
            self._window.workspace_stack.removeWidget(dialog)
            self._window.input_area.text_input.setFocus()
            self._window.window_state_presenter.refresh_menu_action_states()
        dialog.deleteLater()
        if self._close_window_after_save:
            self._close_window_after_save = False
            QTimer.singleShot(0, self._window.close)

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
            self._close_window_after_save = False
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
        if result.failed_domains:
            self._close_window_after_save = False
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
            if hasattr(host, "apply_shortcuts"):
                host.apply_shortcuts()
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

        was_collapsed = host.sidebar.collapsed
        sizes = host.splitter.sizes()
        host.sidebar.set_collapsed(not show_sidebar)
        if was_collapsed != host.sidebar.collapsed:
            width = host.sidebar.expanded_width if show_sidebar else 56
            sizes[1] = max(1, sizes[1] - width + sizes[0])
            sizes[0] = width
            host.splitter.setSizes(sizes)
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
        sizes = self._window.splitter.sizes()
        sidebar = self._window.sidebar
        if sidebar.collapsed:
            sizes[1] = max(1, sizes[1] - sidebar.expanded_width + sizes[0])
            sizes[0] = sidebar.expanded_width
        else:
            sidebar.expanded_width = sizes[0]
        self._persist_splitter_layout('splitter_sizes', sizes, 'splitter')

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
                host.sidebar.expanded_width = max(180, min(320, splitter_sizes[0]))
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
            QCoreApplication.translate("AboutContent", "关于 {name}").format(name=PRODUCT_NAME),
            about_dialog_html(),
        )

    # ------------------------------------------------------------------
    # Release (stable update) checks
    # ------------------------------------------------------------------

    def check_for_updates(self, *, manual: bool = True) -> None:
        """Start one non-blocking stable Release check."""
        host = self._window
        if self._release_job is not None or self._settings_job is not None:
            return
        settings = host.app_settings or {}
        if not manual and not update_check_due(settings, now=time.time()):
            return

        host.app_settings["update_last_checked_at"] = time.time()
        try:
            host.services.app_settings_service.save(host.app_settings)
        except Exception as exc:
            logger.debug("Failed to persist update check timestamp: %s", exc)

        self.set_release_checking(True)
        job = BackgroundJob(
            lambda: host.services.release_checker.check(current_version=__version__),
        )
        self._release_job = job
        job.signals.finished.connect(
            lambda result, error, job=job: self._finish_release_check(job, result, error)
        )
        QThreadPool.globalInstance().start(job)

    def _finish_release_check(self, job: BackgroundJob, result, error) -> None:
        if self._release_job is not job:
            return
        self._release_job = None
        if error is not None:
            result = ReleaseCheckResult(
                status="error",
                current_version=__version__,
                error=f"暂时无法检查更新：{error}",
            )
        if not isinstance(result, ReleaseCheckResult):
            result = ReleaseCheckResult(
                status="error",
                current_version=__version__,
                error="更新检查返回了无效结果。",
            )
        self._release_result = result
        self.set_release_checking(False)
        self._apply_release_result()

    def _apply_release_result(self) -> None:
        host = self._window
        result = self._release_result
        release = getattr(result, "release", None) if result is not None else None
        ignored_tag = str((host.app_settings or {}).get("update_ignored_tag", "") or "").strip()
        available = bool(
            result is not None
            and result.status == "available"
            and release is not None
            and str(getattr(release, "tag_name", "") or "").strip() != ignored_tag
        )
        button = getattr(host, "update_available_btn", None)
        if button is not None:
            button.setVisible(available)
            if available and release is not None:
                button.setToolTip(f"发现新版本 {release.tag_name}，点击打开 Release")
            else:
                button.setToolTip("检查 PyCat 稳定版本更新")
        self.apply_release_result(result, ignored_tag=ignored_tag)

    def open_release_url(self, url: str = "") -> None:
        """Open only the public PyCat Release URL in the system browser."""
        candidate = str(url or "").strip()
        release = getattr(self._release_result, "release", None)
        if not candidate:
            candidate = str(getattr(release, "html_url", "") or STABLE_RELEASE_PAGE).strip()
        if not candidate.startswith("https://github.com/nanhuayu/pycat"):
            candidate = STABLE_RELEASE_PAGE
        QDesktopServices.openUrl(QUrl(candidate))

    def ignore_release(self, tag_name: str) -> None:
        host = self._window
        release = getattr(self._release_result, "release", None)
        tag = str(tag_name or "").strip()
        if release is None or not tag or tag != str(getattr(release, "tag_name", "") or "").strip():
            return
        host.app_settings["update_ignored_tag"] = tag
        try:
            host.services.app_settings_service.save(host.app_settings)
        except Exception as exc:
            logger.debug("Failed to persist ignored Release: %s", exc)
        self._apply_release_result()

    def abandon_release_job(self) -> None:
        if self._release_job is not None:
            self._release_job.abandon()
            self._release_job = None

    def set_release_checking(self, checking: bool) -> None:
        dialog = self._settings_dialog
        page = dialog.created_pages.get("about") if dialog is not None else None
        setter = getattr(page, "set_release_checking", None)
        if callable(setter):
            setter(bool(checking))

    def apply_release_result(self, result, *, ignored_tag: str = "") -> None:
        dialog = self._settings_dialog
        page = dialog.created_pages.get("about") if dialog is not None else None
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
