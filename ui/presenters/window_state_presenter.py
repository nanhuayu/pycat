"""Window state presenter.

Projects app/runtime state onto MainWindow chrome widgets, keeping
header/menu/input-sync logic out of the window shell.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ui.main_window import MainWindow

logger = logging.getLogger(__name__)


class WindowStatePresenter:
    """Owns header/menu/input state synchronization for MainWindow."""

    def __init__(self, host: MainWindow) -> None:
        self._host = host

    def sync_input_enabled(self) -> None:
        """Enable/disable input for the currently selected conversation only."""
        host = self._host
        try:
            if not host.current_conversation:
                host.input_area.set_streaming_state(False)
                host.services.app_coordinator.clear_current_conversation()
                self.refresh_menu_action_states()
                return
            is_streaming = host.message_runtime.is_streaming(host.current_conversation.id)
            host.input_area.set_streaming_state(is_streaming)
            host.services.app_coordinator.set_streaming(
                host.current_conversation.id,
                is_streaming=is_streaming,
            )
            self.refresh_menu_action_states()
        except Exception as e:
            logger.debug("Failed to sync input enabled state: %s", e)
    
    def apply_bootstrap_state(self, bootstrap_state) -> None:
        host = self._host
        host.app_settings = dict(getattr(bootstrap_state, 'settings', {}) or {})
        host.services.tool_manager.update_permissions(host.app_settings)
        try:
            host.input_area.set_app_settings(host.app_settings)
        except Exception as e:
            logger.debug("Failed to sync app settings into input area: %s", e)
        host.settings_presenter.apply_proxy()
        try:
            host.services.client.set_timeout(
                float(host.app_settings.get("llm_timeout_seconds", 600.0) or 600.0)
            )
        except Exception as e:
            logger.debug("Failed to sync LLM timeout from settings: %s", e)

        host.providers = list(getattr(bootstrap_state, 'providers', []) or [])
        default_chat_model = str(host.app_settings.get("default_chat_model", "") or "").strip()
        host.input_area.set_providers(
            host.providers,
            selected_model_ref=default_chat_model,
            emit_signal=False,
        )
        try:
            current_model_ref = default_chat_model or host.services.app_coordinator.build_model_ref(
                providers=host.providers,
                provider_id=host.input_area.get_selected_provider_id(),
                model=host.input_area.get_selected_model(),
            )
            host.chat_view.set_model_options(host.providers, current_model_ref=current_model_ref)
        except Exception as e:
            logger.debug("Failed to sync header model options from bootstrap: %s", e)
        try:
            host.input_area.apply_mode_policy(apply_defaults=True)
        except Exception as e:
            logger.debug("Failed to apply initial mode defaults: %s", e)
        self.sync_chat_header_from_input()

        conversations = list(getattr(bootstrap_state, 'conversations', []) or [])
        host.sidebar.update_conversations(conversations)
        host.services.app_coordinator.sync_catalog(
            providers=host.providers,
            conversation_count=len(conversations),
        )
        host.services.app_coordinator.remember_current_conversation(
            None,
            providers=host.providers,
            app_settings=host.app_settings,
            is_streaming=False,
        )

        host.settings_presenter.apply_bootstrap_shell_state(
            show_stats=bool(getattr(bootstrap_state, 'show_stats', True)),
            splitter_sizes=getattr(bootstrap_state, 'splitter_sizes', None),
            chat_splitter_sizes=getattr(bootstrap_state, 'chat_splitter_sizes', None),
        )

    def shutdown(self) -> None:
        host = self._host
        try:
            if callable(getattr(host, "unsubscribe_app_state", None)):
                host.unsubscribe_app_state()
        except Exception as e:
            logger.debug("Failed to unsubscribe app state listener on exit: %s", e)
        try:
            host.services.channel_runtime.stop()
        except Exception as e:
            logger.debug("Failed to stop channel runtime on exit: %s", e)
        try:
            asyncio.run(host.services.tool_manager.shutdown())
        except Exception as e:
            logger.debug("Failed to shutdown MCP sessions on exit: %s", e)

    def on_app_state_store_changed(self) -> None:
        host = self._host
        try:
            state = host.services.app_coordinator.store.get_state()
        except Exception as e:
            logger.debug("Failed to read app state from store: %s", e)
            return

        try:
            stats_panel = getattr(host, "stats_panel", None)
            if stats_panel is not None and hasattr(stats_panel, "update_app_state"):
                stats_panel.update_app_state(state)
        except Exception as e:
            logger.debug("Failed to project app state to stats panel: %s", e)

        try:
            current_id = str(getattr(host.current_conversation, "id", "") or "")
            if state.current_conversation_id and state.current_conversation_id == current_id:
                if state.model_ref:
                    host.chat_view.update_header(state.model_ref, msg_count=int(state.message_count or 0))
                host.input_area.set_streaming_state(bool(state.is_streaming))
            elif not state.current_conversation_id:
                host.input_area.set_streaming_state(False)
        except Exception as e:
            logger.debug("Failed to apply app state to main window: %s", e)

        self.refresh_menu_action_states()

    def sync_chat_header_from_input(
        self,
        provider_id: str | None = None,
        model: str | None = None,
    ) -> None:
        host = self._host
        selected_provider_id = provider_id if provider_id is not None else host.input_area.get_selected_provider_id()
        selected_model = model if model is not None else host.input_area.get_selected_model()

        msg_count = 0
        try:
            if host.current_conversation:
                msg_count = len(getattr(host.current_conversation, 'messages', []) or [])
        except Exception as e:
            logger.debug("Failed to get message count for header sync: %s", e)

        try:
            provider_name = ''
            if host.current_conversation:
                provider_name = str(getattr(host.current_conversation, 'provider_name', '') or '')
            model_ref = host.services.app_coordinator.build_model_ref(
                providers=host.providers,
                provider_id=selected_provider_id,
                provider_name=provider_name,
                model=selected_model or '',
            )
            try:
                host.chat_view.set_model_options(host.providers, current_model_ref=model_ref)
            except Exception as e:
                logger.debug("Failed to sync header model options: %s", e)
            host.chat_view.update_header(model_ref, msg_count=msg_count)
        except Exception as e:
            logger.debug("Failed to sync chat header from input: %s", e)

        self.refresh_menu_action_states()

    def sync_chat_header_for_current_conversation(
        self,
        conversation_id: str | None = None,
    ) -> None:
        host = self._host
        conversation = host.current_conversation
        if not conversation:
            self.sync_chat_header_from_input()
            return
        if conversation_id and conversation.id != conversation_id:
            return
        self.sync_chat_header_from_input(
            provider_id=str(getattr(conversation, 'provider_id', '') or '') or None,
            model=str(getattr(conversation, 'model', '') or '') or None,
        )

    def refresh_menu_action_states(self) -> None:
        host = self._host
        has_conversation = bool(host.current_conversation)
        has_messages = bool(has_conversation and getattr(host.current_conversation, 'messages', None))
        try:
            app_state = host.services.app_coordinator.store.get_state()
            current_id = str(getattr(host.current_conversation, 'id', '') or '')
            is_streaming = bool(
                has_conversation
                and app_state.current_conversation_id == current_id
                and app_state.is_streaming
            )
        except Exception:
            is_streaming = bool(has_conversation and host.message_runtime.is_streaming(host.current_conversation.id))

        for action_name, enabled in (
            ('export_markdown_action', has_conversation),
            ('export_json_action', has_conversation),
            ('duplicate_conversation_action', has_conversation),
            ('delete_conversation_action', has_conversation),
            ('conversation_settings_action', has_conversation),
            ('provider_settings_action', True),
            ('compact_action', has_messages),
            ('cancel_action', is_streaming),
        ):
            action = getattr(host, action_name, None)
            if action is not None:
                action.setEnabled(bool(enabled))