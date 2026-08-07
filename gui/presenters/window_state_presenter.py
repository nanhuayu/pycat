"""Window state presenter.

Projects app/runtime state onto MainWindow chrome widgets, keeping
header/menu/input-sync logic out of the window shell.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PyQt6.QtCore import QThreadPool

from core.llm.token_budget import build_token_usage_snapshot
from gui.runtime.background_job import BackgroundJob
from models.contracts.config import AppConfig

if TYPE_CHECKING:
    from gui.main_window import MainWindow

logger = logging.getLogger(__name__)


def _compact_threshold_ratio(settings: dict) -> float:
    return AppConfig.from_dict(settings or {}).context.compression_policy.token_threshold_ratio


class WindowStatePresenter:
    """Owns header/menu/input state synchronization for MainWindow."""

    def __init__(self, host: MainWindow) -> None:
        self._host = host

    def _is_compacting(self, conversation_id: str) -> bool:
        presenter = getattr(self._host, "conversation_presenter", None)
        checker = getattr(presenter, "is_compacting", None)
        return bool(callable(checker) and checker(conversation_id))

    def _is_maintaining(self, conversation_id: str) -> bool:
        presenter = getattr(self._host, "conversation_presenter", None)
        checker = getattr(presenter, "is_maintaining", None)
        return bool(callable(checker) and checker(conversation_id))

    def _is_submitting(self, conversation_id: str) -> bool:
        presenter = getattr(self._host, "message_presenter", None)
        checker = getattr(presenter, "is_submitting", None)
        return bool(callable(checker) and checker(conversation_id))

    def sync_runtime_state(self, conversation_id: str | None = None) -> None:
        """Project the current conversation's runtime or lifecycle operation."""
        host = self._host
        current = host.current_conversation
        current_id = str(getattr(current, "id", "") or "")
        if not current_id or (conversation_id and current_id != str(conversation_id)):
            return
        runtime = getattr(host, "message_runtime", None)
        get_state = getattr(runtime, "get_state", None)
        stream_state = get_state(current_id) if callable(get_state) else None
        pending_guidance = 0
        pending_count = getattr(runtime, "pending_guidance_count", None)
        if callable(pending_count):
            pending_guidance = int(pending_count(current_id) or 0)
        operation = ""
        active_operation = getattr(
            getattr(host, "conversation_presenter", None),
            "active_operation",
            None,
        )
        if callable(active_operation):
            operation = str(active_operation(current_id) or "")
        else:
            shared_operation = getattr(
                getattr(host.services, "conv_service", None),
                "active_operation",
                None,
            )
            if callable(shared_operation):
                operation = str(shared_operation(current_id) or "")
        updater = getattr(getattr(host, "chat_view", None), "update_runtime_state", None)
        if callable(updater):
            updater(
                stream_state,
                operation=operation,
                pending_guidance=pending_guidance,
            )

    def sync_input_enabled(self) -> None:
        """Enable/disable input for the currently selected conversation only."""
        host = self._host
        try:
            set_permission_enabled = getattr(host.input_area, "set_permission_preset_enabled", None)
            if callable(set_permission_enabled):
                set_permission_enabled(bool(host.current_conversation))
            if not host.current_conversation:
                # ``InputArea`` may have been disabled by a delete/migration
                # operation.  Restoring only its child editor is insufficient:
                # a disabled parent keeps every child disabled in Qt.
                set_input_enabled = getattr(host.input_area, "setEnabled", None)
                if callable(set_input_enabled):
                    set_input_enabled(True)
                host.input_area.set_streaming_state(False)
                set_revision_enabled = getattr(
                    getattr(host, "chat_view", None),
                    "set_revision_enabled",
                    None,
                )
                if callable(set_revision_enabled):
                    set_revision_enabled(False)
                set_snapshot = getattr(host.input_area, "set_token_snapshot", None)
                if callable(set_snapshot):
                    set_snapshot(None)
                set_context_busy = getattr(host.input_area, "set_context_busy", None)
                if callable(set_context_busy):
                    set_context_busy(False)
                set_submission_busy = getattr(host.input_area, "set_submission_busy", None)
                if callable(set_submission_busy):
                    set_submission_busy(False)
                host.services.app_coordinator.clear_current_conversation()
                self.refresh_menu_action_states()
                return
            is_streaming = host.message_runtime.is_streaming(host.current_conversation.id)
            is_compacting = self._is_compacting(host.current_conversation.id)
            is_maintaining = self._is_maintaining(host.current_conversation.id)
            is_submitting = self._is_submitting(host.current_conversation.id)
            host.input_area.set_streaming_state(is_streaming)
            set_revision_enabled = getattr(
                getattr(host, "chat_view", None),
                "set_revision_enabled",
                None,
            )
            if callable(set_revision_enabled):
                set_revision_enabled(not is_streaming and not is_maintaining)
            set_context_busy = getattr(host.input_area, "set_context_busy", None)
            if callable(set_context_busy):
                set_context_busy(is_compacting)
            set_submission_busy = getattr(host.input_area, "set_submission_busy", None)
            if callable(set_submission_busy):
                set_submission_busy(is_submitting)
            # Compact keeps its dedicated busy projection; migration and
            # deletion disable the composer as a whole without pretending to
            # be model generation or context compression.
            set_input_enabled = getattr(host.input_area, "setEnabled", None)
            if callable(set_input_enabled):
                set_input_enabled(not is_maintaining or is_compacting)
            work_dir_button = getattr(getattr(host, "chat_view", None), "work_dir_btn", None)
            if work_dir_button is not None:
                work_dir_button.setEnabled(not is_maintaining)
            set_mutations_enabled = getattr(host.inspector_panel, "set_mutations_enabled", None)
            if callable(set_mutations_enabled):
                set_mutations_enabled(not is_streaming and not is_compacting and not is_maintaining)
            host.services.app_coordinator.set_streaming(
                host.current_conversation.id,
                is_streaming=is_streaming,
            )
            self.sync_runtime_state(host.current_conversation.id)
            self.refresh_menu_action_states()
        except Exception as e:
            logger.debug("Failed to sync input enabled state: %s", e)
    
    def apply_bootstrap_state(self, bootstrap_state) -> None:
        host = self._host
        host.app_settings = dict(getattr(bootstrap_state, 'settings', {}) or {})
        apply_window_size = getattr(host, "apply_window_size", None)
        if callable(apply_window_size):
            apply_window_size(host.app_settings.get("main_window_size"))
        try:
            host.container.apply_runtime_configuration(host.app_settings)
        except Exception as e:
            logger.debug("Failed to apply bootstrap runtime configuration: %s", e)
        try:
            host.input_area.set_app_settings(host.app_settings)
        except Exception as e:
            logger.debug("Failed to sync app settings into input area: %s", e)
        try:
            host.tray_controller.set_enabled(bool(host.app_settings.get("close_to_tray", True)))
        except Exception as e:
            logger.debug("Failed to sync tray setting: %s", e)
        host.settings_presenter.apply_proxy()

        host.providers = list(getattr(bootstrap_state, 'providers', []) or [])
        default_chat_model = str(host.app_settings.get("default_chat_model", "") or "").strip()
        host.input_area.set_providers(
            host.providers,
            selected_model_ref=default_chat_model,
            emit_signal=False,
        )
        self.sync_chat_header_from_input()

        conversations = list(getattr(bootstrap_state, 'conversations', []) or [])
        host.sidebar.update_conversations(conversations)
        sidebar_setter = getattr(host.sidebar, "set_streaming_conversations", None)
        runtime = getattr(host, "message_runtime", None)
        runtime_ids = getattr(runtime, "streaming_conversation_ids", None)
        if callable(sidebar_setter):
            sidebar_setter(runtime_ids() if callable(runtime_ids) else ())
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
            show_sidebar=bool(getattr(bootstrap_state, 'show_sidebar', True)),
            show_stats=bool(getattr(bootstrap_state, 'show_stats', False)),
            splitter_sizes=getattr(bootstrap_state, 'splitter_sizes', None),
            chat_splitter_sizes=getattr(bootstrap_state, 'chat_splitter_sizes', None),
        )

    def shutdown(self, on_finished=None) -> None:
        host = self._host
        if getattr(self, "_shutdown_job", None) is not None:
            return
        try:
            if callable(getattr(host, "unsubscribe_app_state", None)):
                host.unsubscribe_app_state()
        except Exception as e:
            logger.debug("Failed to unsubscribe app state listener on exit: %s", e)
        def operation():
            errors: list[str] = []
            try:
                host.services.channel_gateway.stop()
            except Exception as exc:
                errors.append(f"channel: {exc}")
                logger.debug("Failed to stop channel gateway on exit: %s", exc)
            try:
                import asyncio

                asyncio.run(host.services.tool_manager.shutdown())
            except Exception as exc:
                errors.append(f"tools: {exc}")
                logger.debug("Failed to shutdown MCP sessions on exit: %s", exc)
            return "; ".join(errors)

        job = BackgroundJob(operation)
        self._shutdown_job = job

        def finish(result, error) -> None:
            if getattr(self, "_shutdown_job", None) is not job:
                return
            self._shutdown_job = None
            if error is not None:
                logger.debug("Application shutdown worker failed: %s", error)
                shutdown_error = str(error)
            else:
                shutdown_error = str(result or "")
            if callable(on_finished):
                on_finished(shutdown_error or None)

        job.signals.finished.connect(finish)
        QThreadPool.globalInstance().start(job)

    def abandon_shutdown(self) -> None:
        """Suppress a late shutdown callback during a forced close."""
        job = getattr(self, "_shutdown_job", None)
        if job is None:
            return
        try:
            job.abandon()
        finally:
            self._shutdown_job = None

    def on_app_state_store_changed(self) -> None:
        host = self._host
        try:
            state = host.services.app_coordinator.store.get_state()
        except Exception as e:
            logger.debug("Failed to read app state from store: %s", e)
            return

        try:
            inspector_panel = getattr(host, "inspector_panel", None)
            if inspector_panel is not None and hasattr(inspector_panel, "update_app_state"):
                inspector_panel.update_app_state(state)
        except Exception as e:
            logger.debug("Failed to project app state to stats panel: %s", e)

        try:
            current_id = str(getattr(host.current_conversation, "id", "") or "")
            if state.current_conversation_id and state.current_conversation_id == current_id:
                token_snapshot = None
                if host.current_conversation is not None:
                    token_snapshot = build_token_usage_snapshot(
                        host.current_conversation,
                        providers=getattr(host, "providers", None),
                        compact_threshold_ratio=_compact_threshold_ratio(host.app_settings),
                    )
                if state.model_ref:
                    host.chat_view.update_header(
                        state.model_ref,
                        msg_count=int(state.message_count or 0),
                    )
                set_snapshot = getattr(host.input_area, "set_token_snapshot", None)
                if callable(set_snapshot):
                    set_snapshot(token_snapshot)
                host.input_area.set_streaming_state(bool(state.is_streaming))
                is_compacting = self._is_compacting(current_id)
                set_context_busy = getattr(host.input_area, "set_context_busy", None)
                if callable(set_context_busy):
                    set_context_busy(is_compacting)
                set_mutations_enabled = getattr(host.inspector_panel, "set_mutations_enabled", None)
                if callable(set_mutations_enabled):
                    is_maintaining = self._is_maintaining(current_id)
                    set_mutations_enabled(
                        not bool(state.is_streaming) and not is_compacting and not is_maintaining
                    )
            elif not current_id:
                host.input_area.set_streaming_state(False)
                set_snapshot = getattr(host.input_area, "set_token_snapshot", None)
                if callable(set_snapshot):
                    set_snapshot(None)
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
            token_snapshot = None
            if host.current_conversation is not None:
                token_snapshot = build_token_usage_snapshot(
                    host.current_conversation,
                    providers=host.providers,
                    provider_id=selected_provider_id,
                    model_id=selected_model or "",
                    compact_threshold_ratio=_compact_threshold_ratio(host.app_settings),
                )
            set_snapshot = getattr(host.input_area, "set_token_snapshot", None)
            if callable(set_snapshot):
                set_snapshot(token_snapshot)
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
        is_compacting = bool(
            has_conversation
            and self._is_compacting(host.current_conversation.id)
        )
        is_maintaining = bool(
            has_conversation
            and self._is_maintaining(host.current_conversation.id)
        )

        for action_name, enabled in (
            ('export_markdown_action', has_conversation),
            ('export_json_action', has_conversation),
            ('delete_conversation_action', has_conversation and not is_maintaining),
            ('conversation_settings_action', has_conversation and not is_maintaining),
            ('provider_settings_action', True),
            ('compact_action', has_messages and not is_streaming and not is_maintaining),
            ('cancel_action', is_streaming),
        ):
            action = getattr(host, action_name, None)
            if action is not None:
                action.setEnabled(bool(enabled))

