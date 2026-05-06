"""Settings presenter - handles theme, proxy, and settings/provider UI flows."""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from PyQt6.QtWidgets import QMessageBox

from core.config.schema import ChannelConfig
from models.provider import Provider
from ui.dialogs.provider_config_dialog import ProviderConfigDialog
from ui.settings.settings_dialog import SettingsDialog

if TYPE_CHECKING:
    from ui.main_window import MainWindow

logger = logging.getLogger(__name__)


class SettingsPresenter:
    """Handles application settings, provider catalog, and shell-level settings UI."""

    def __init__(self, window: MainWindow):
        self._window = window

    def apply_theme(self) -> None:
        """Apply theme based on app settings."""
        try:
            project_root = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            theme = (self._window.app_settings.get('theme') or 'light').lower()
            theme_file = 'light_theme.qss' if theme == 'light' else 'dark_theme.qss'
            theme_path = os.path.join(project_root, 'assets', 'styles', theme_file)
            base_theme_path = os.path.join(project_root, 'assets', 'styles', 'base.qss')

            parts: list[str] = []
            if os.path.exists(base_theme_path):
                with open(base_theme_path, 'r', encoding='utf-8') as f:
                    parts.append(f.read())
            if os.path.exists(theme_path):
                with open(theme_path, 'r', encoding='utf-8') as f:
                    parts.append(f.read())

            if parts:
                self._window.setStyleSheet("\n\n".join(parts))
        except Exception as e:
            logger.error("Error loading theme: %s", e)

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
        host.providers = host.services.provider_catalog_service.snapshot(providers)
        if persist:
            host.services.provider_catalog_service.save(host.providers)
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
            host.chat_view.set_model_options(host.providers, current_model_ref=current_ref)
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
        provider, _provider_index = host.services.provider_catalog_service.select_or_first(
            host.providers,
            provider_id,
        )

        dialog = ProviderConfigDialog(
            provider,
            provider_service=host.services.provider_service,
            parent=host,
        )
        dialog.setWindowTitle(f"配置服务商 - {provider.name if provider else '新建'}")
        if dialog.exec():
            updated_provider = dialog.get_provider()
            next_providers = host.services.provider_catalog_service.upsert(host.providers, updated_provider)
            self.apply_provider_catalog(
                next_providers,
                selected_provider_id=updated_provider.id,
                selected_model=updated_provider.default_model,
            )

    def open_settings(self) -> None:
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
            storage_service=host.services.storage,
            channel_runtime=host.services.channel_runtime,
            parent=host,
            work_dir=work_dir,
        )
        if dialog.exec():
            update = dialog.build_update()
            selected_provider_id = host.input_area.get_selected_provider_id()
            selected_model = host.input_area.get_selected_model()
            default_chat_model = str(update.settings_patch.get("default_chat_model", "") or "").strip()
            self.apply_provider_catalog(
                list(update.providers),
                selected_provider_id=selected_provider_id,
                selected_model=selected_model,
                selected_model_ref=default_chat_model if not host.current_conversation else "",
            )
            preferred_channel_session_id = dialog.get_preferred_channel_session_id()
            try:
                next_channels, preferred_channel_session_id = self._materialize_channel_sessions(
                    update.settings_patch.get("channels", []),
                    preferred_session_id=preferred_channel_session_id,
                )
                if next_channels:
                    update.settings_patch["channels"] = next_channels
            except Exception as e:
                logger.debug("Failed to materialize channel test sessions from settings: %s", e)

            host.app_settings = host.services.app_settings_service.apply_update(
                host.app_settings,
                update,
            )

            self.apply_proxy()
            try:
                host.services.client.set_timeout(float(host.app_settings.get('llm_timeout_seconds', 600.0) or 600.0))
            except Exception as e:
                logger.debug("Failed to apply updated LLM timeout: %s", e)

            host.services.tool_manager.update_permissions(host.app_settings)
            host.services.app_settings_service.save(host.app_settings)

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
                host.services.channel_runtime.start(host.app_settings)
            except Exception as e:
                logger.debug("Failed to reload channel runtime after settings update: %s", e)

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

            host.stats_panel.setVisible(host.app_settings['show_stats'])
            host.toggle_stats_action.setChecked(host.app_settings['show_stats'])
            self.apply_theme()

    def _materialize_channel_sessions(
        self,
        channels_payload: list,
        *,
        preferred_session_id: str = "",
    ) -> tuple[list[dict], str]:
        host = self._window
        focus_session_id = str(preferred_session_id or "").strip()
        materialized: list[dict] = []

        for raw_channel in list(channels_payload or []):
            channel = raw_channel if isinstance(raw_channel, ChannelConfig) else ChannelConfig.from_dict(raw_channel)
            existing_session_id = str(getattr(channel, "session_id", "") or "").strip()
            if existing_session_id:
                try:
                    existing_conversation = host.services.conv_service.load(existing_session_id)
                    if existing_conversation is None:
                        if focus_session_id == existing_session_id:
                            channel = host.services.channel_runtime.ensure_channel_session(channel, persist=True)
                        else:
                            channel = self._clear_channel_session_id(channel)
                    elif self._is_empty_manual_channel_session(existing_conversation) and focus_session_id != existing_session_id:
                        host.services.conv_service.delete(existing_session_id)
                        channel = self._clear_channel_session_id(channel)
                    elif not focus_session_id:
                        focus_session_id = existing_session_id
                except Exception as e:
                    logger.debug("Failed to validate channel session for %s: %s", getattr(channel, "id", ""), e)
            materialized.append(channel.to_dict())

        return materialized, focus_session_id

    @staticmethod
    def _clear_channel_session_id(channel: ChannelConfig) -> ChannelConfig:
        payload = channel.to_dict()
        payload["session_id"] = ""
        return ChannelConfig.from_dict(payload)

    @staticmethod
    def _is_empty_manual_channel_session(conversation) -> bool:
        messages = list(getattr(conversation, "messages", []) or [])
        if messages:
            return False
        settings = getattr(conversation, "settings", {}) or {}
        binding = settings.get("channel_binding") if isinstance(settings, dict) else None
        return bool(isinstance(binding, dict) and binding.get("manual_test_session"))

    def toggle_stats_panel(self, visible: bool) -> None:
        host = self._window
        host.stats_panel.setVisible(visible)
        host.app_settings['show_stats'] = bool(visible)
        host.services.app_settings_service.save(host.app_settings)

    def persist_main_splitter_layout(self, _pos: int, _index: int) -> None:
        self._persist_splitter_layout('splitter_sizes', self._window.splitter.sizes(), 'splitter')

    def persist_chat_splitter_layout(self, _pos: int, _index: int) -> None:
        self._persist_splitter_layout('chat_splitter_sizes', self._window.chat_splitter.sizes(), 'chat splitter')
    
    def apply_bootstrap_shell_state(
        self,
        *,
        show_stats: bool,
        splitter_sizes: list[int] | None,
        chat_splitter_sizes: list[int] | None,
    ) -> None:
        host = self._window
        host.stats_panel.setVisible(bool(show_stats))
        host.toggle_stats_action.setChecked(bool(show_stats))

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
            "关于 PyCat Agent",
            "<h2>PyCat Agent</h2>"
            "<p>LLM chat / agent / tools 一体化桌面工作台</p>"
            "<p>功能:</p>"
            "<ul>"
            "<li>统一支持 Chat、Agent 与 Tools 工作流</li>"
            "<li>多服务商支持 (OpenAI, Claude, Ollama 等)</li>"
            "<li>会话管理与 JSON 导入/导出</li>"
            "<li>消息编辑与图片支持</li>"
            "<li>思考模式支持</li>"
            "<li>Token 统计与性能指标</li>"
            "</ul>"
            "<p>基于 PyQt6 构建</p>"
            "<p>许可证：AGPL-3.0</p>"
        )

    def _persist_splitter_layout(self, key: str, sizes: list[int], label: str) -> None:
        host = self._window
        try:
            host.app_settings[key] = [int(x) for x in sizes]
            host.services.app_settings_service.save(host.app_settings)
        except Exception as e:
            logger.debug("Failed to persist %s layout: %s", label, e)
