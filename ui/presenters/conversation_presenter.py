"""Conversation lifecycle presenter.

Extracts conversation CRUD + selection logic from MainWindow,
reducing it by ~170 lines.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from PyQt6.QtWidgets import QMessageBox

from core.state.services.task_service import TaskService
from core.app.state import ConversationSelection
from models.conversation import Conversation
from models.provider import split_model_ref
from ui.dialogs.conversation_settings_dialog import ConversationSettingsDialog
from ui.presenters.conversation_command_presenter import ConversationCommandPresenter

if TYPE_CHECKING:
    from ui.main_window import MainWindow

logger = logging.getLogger(__name__)


class ConversationPresenter:
    """Handles conversation selection, creation, import, delete, duplicate."""

    def __init__(self, host: MainWindow) -> None:
        self._host = host
        self._command_presenter = ConversationCommandPresenter(
            host,
            create_new_conversation=self.new,
            compact_current=self.compact_current,
        )

    def seed_from_input(self, conversation: Conversation) -> Conversation:
        host = self._host
        selection = self._capture_selection()
        host.services.app_coordinator.apply_selection(conversation, selection)
        host.services.app_coordinator.remember_current_conversation(
            conversation,
            providers=host.providers,
            app_settings=host.app_settings,
            is_streaming=bool(
                getattr(conversation, 'id', '')
                and host.message_runtime.is_streaming(conversation.id)
            ),
        )
        return conversation

    def ensure_current_conversation_shell(self) -> Conversation:
        host = self._host
        selection = self._capture_selection()
        host.current_conversation = host.services.app_coordinator.ensure_conversation(
            host.current_conversation,
            selection=selection,
        )
        host.services.app_coordinator.remember_current_conversation(
            host.current_conversation,
            providers=host.providers,
            app_settings=host.app_settings,
            is_streaming=bool(
                getattr(host.current_conversation, 'id', '')
                and host.message_runtime.is_streaming(host.current_conversation.id)
            ),
        )
        return host.current_conversation

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def select(self, conversation_id: str) -> None:
        host = self._host
        conversation = host.services.conv_service.load(conversation_id)
        if not conversation:
            return

        stream_state = host.message_runtime.get_state(conversation.id)
        host.services.app_coordinator.remember_current_conversation(
            conversation,
            providers=host.providers,
            app_settings=host.app_settings,
            is_streaming=bool(stream_state),
        )

        host.is_syncing_input_selection = True
        try:
            host.current_conversation = conversation
            host.chat_view.load_conversation(conversation)
            host.stats_panel.update_stats(conversation)
            try:
                host.stats_panel.update_runtime_state(stream_state)
            except Exception as e:
                logger.debug("Failed to restore runtime state during select: %s", e)

            # Sync per-conversation toggles
            show_thinking_default = bool(host.app_settings.get("show_thinking", True))
            show_thinking = bool(
                (conversation.settings or {}).get("show_thinking", show_thinking_default)
            )
            host.input_area.set_show_thinking(show_thinking)

            # Sync provider (so model list is populated), then model
            provider_name = ""
            resolved_provider = host.services.conv_service.resolve_provider(
                host.providers,
                conversation.provider_id,
                getattr(conversation, "provider_name", ""),
            )
            if resolved_provider is not None:
                provider_name = str(getattr(resolved_provider, "name", "") or "")
                host.input_area.set_provider_model_selection(
                    provider_id=str(getattr(resolved_provider, "id", "") or ""),
                    model=conversation.model,
                    emit_signal=False,
                )
            elif conversation.model:
                host.input_area.set_provider_model_selection(model=conversation.model, emit_signal=False)

            # Sync mode selection
            try:
                mode_slug = str(getattr(conversation, "mode", "") or "")
                host.input_area.set_mode_selection(mode_slug, apply_defaults=False)
            except Exception as e:
                logger.debug("Failed to sync mode selection during select: %s", e)

            try:
                conv_settings = conversation.settings or {}
                default_search, default_mcp = host.input_area.get_mode_default_tool_flags()
                host.input_area.set_tool_toggles(
                    enable_mcp=bool(conv_settings.get("enable_mcp", default_mcp)),
                    enable_search=bool(conv_settings.get("enable_search", default_search)),
                )
            except Exception as e:
                logger.debug("Failed to sync MCP/Search toggles during select: %s", e)

            # Update chat header
            host.chat_view.update_header(
                host.services.conv_service.build_model_ref(conversation, host.providers),
                msg_count=len(conversation.messages),
            )
            work_dir = getattr(conversation, "work_dir", "")
            host.chat_view.update_work_dir(work_dir)
            host.input_area.set_work_dir(work_dir)
            host.input_area.set_conversation(conversation)

            # Restore streaming UI if this conversation is currently generating
            if stream_state:
                host.chat_view.start_streaming_response(model=stream_state.model)
                host.chat_view.restore_streaming_state(
                    visible_text=stream_state.visible_text,
                    thinking_text=stream_state.thinking_text,
                )
            if hasattr(host.chat_view, "update_runtime_state"):
                host.chat_view.update_runtime_state(stream_state)

            host.window_state_presenter.sync_input_enabled()
        finally:
            host.is_syncing_input_selection = False

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def new(self) -> None:
        host = self._host
        selection = self._capture_selection(prefer_default_model=True)
        host.current_conversation = host.services.app_coordinator.create_conversation(selection)
        try:
            applied = host.input_area.set_provider_model_selection(
                provider_id=str(getattr(host.current_conversation, "provider_id", "") or ""),
                model=str(getattr(host.current_conversation, "model", "") or ""),
                emit_signal=False,
            )
            if applied is False and hasattr(host.input_area, "set_providers"):
                host.input_area.set_providers(
                    list(host.providers),
                    selected_provider_id=str(getattr(host.current_conversation, "provider_id", "") or ""),
                    selected_model=str(getattr(host.current_conversation, "model", "") or ""),
                    emit_signal=False,
                )
        except Exception as e:
            logger.debug("Failed to sync provider/model selection for new conversation: %s", e)
        try:
            mode_slug = str(getattr(host.current_conversation, "mode", "") or "chat").strip() or "chat"
            host.input_area.set_mode_selection(mode_slug, apply_defaults=False)
        except Exception as e:
            logger.debug("Failed to sync mode selection for new conversation: %s", e)
        host.services.app_coordinator.remember_current_conversation(
            host.current_conversation,
            providers=host.providers,
            app_settings=host.app_settings,
            is_streaming=False,
        )
        host.chat_view.clear()
        host.stats_panel.update_stats(None)
        host.window_state_presenter.sync_chat_header_from_input(
            provider_id=str(getattr(host.current_conversation, "provider_id", "") or "") or None,
            model=str(getattr(host.current_conversation, "model", "") or "") or None,
        )
        work_dir = str(getattr(host.current_conversation, "work_dir", "") or "")
        host.chat_view.update_work_dir(work_dir)
        host.input_area.set_work_dir(work_dir)
        host.input_area.set_conversation(host.current_conversation)
        try:
            host.services.conv_service.save(host.current_conversation)
            conversations = host.services.conv_service.list_all()
            host.services.app_coordinator.sync_catalog(
                providers=host.providers,
                conversation_count=len(conversations),
            )
        except Exception as e:
            logger.debug("Failed to save new conversation shell: %s", e)
        host.window_state_presenter.sync_input_enabled()

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    def import_from_file(self, file_path: str) -> None:
        host = self._host
        conversation = host.services.conv_service.import_from_file(file_path)
        if conversation:
            conversations = host.services.conv_service.list_all()
            host.sidebar.update_conversations(conversations)
            host.services.app_coordinator.sync_catalog(
                providers=host.providers,
                conversation_count=len(conversations),
            )
            host.sidebar.select_conversation(conversation.id)
            self.select(conversation.id)
            QMessageBox.information(host, "导入成功", f"已导入会话: {conversation.title}")
        else:
            QMessageBox.warning(host, "导入失败", "无法导入会话，请检查 JSON 格式")

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete(self, conversation_id: str) -> None:
        host = self._host
        try:
            asyncio.run(host.services.tool_manager.close_conversation_sessions(conversation_id))
        except Exception as e:
            logger.debug("Failed to close MCP sessions for deleted conversation: %s", e)
        if host.services.conv_service.delete(conversation_id):
            conversations = host.services.conv_service.list_all()
            host.sidebar.update_conversations(conversations)
            host.services.app_coordinator.sync_catalog(
                providers=host.providers,
                conversation_count=len(conversations),
            )

            if host.current_conversation and host.current_conversation.id == conversation_id:
                host.current_conversation = None
                host.services.app_coordinator.clear_current_conversation()
                host.chat_view.clear()
                host.stats_panel.update_stats(None)
                host.window_state_presenter.sync_chat_header_from_input()
                host.chat_view.update_work_dir("")
                host.input_area.set_work_dir("")
                host.input_area.set_conversation(None)
                host.window_state_presenter.sync_input_enabled()

    # ------------------------------------------------------------------
    # Duplicate
    # ------------------------------------------------------------------

    def duplicate(self, conversation_id: str) -> None:
        host = self._host
        src = host.services.conv_service.load(conversation_id)
        if not src:
            QMessageBox.warning(host, "复制失败", "未找到要复制的会话")
            return

        dup = host.services.conv_service.duplicate(src)

        if not host.services.conv_service.save(dup):
            QMessageBox.warning(host, "复制失败", "保存会话副本失败")
            return

        conversations = host.services.conv_service.list_all()
        host.sidebar.update_conversations(conversations)
        host.services.app_coordinator.sync_catalog(
            providers=host.providers,
            conversation_count=len(conversations),
        )
        host.sidebar.select_conversation(dup.id)
        self.select(dup.id)

    def duplicate_current(self) -> None:
        host = self._host
        if not host.current_conversation:
            return
        self.duplicate(host.current_conversation.id)

    def delete_current(self) -> None:
        host = self._host
        if not host.current_conversation:
            return
        self.delete(host.current_conversation.id)

    def open_settings(self) -> None:
        host = self._host
        if not host.current_conversation:
            host.current_conversation = self.ensure_current_conversation_shell()

        dlg = ConversationSettingsDialog(
            host.current_conversation,
            providers=host.providers,
            default_show_thinking=bool(host.app_settings.get('show_thinking', True)),
            parent=host,
        )
        if not dlg.exec():
            return

        update = dlg.build_update()
        host.services.app_coordinator.apply_settings_update(
            host.current_conversation,
            update=update,
            providers=host.providers,
        )
        host.services.conv_service.save(host.current_conversation)
        host.stats_panel.update_stats(host.current_conversation)
        host.services.app_coordinator.remember_current_conversation(
            host.current_conversation,
            providers=host.providers,
            app_settings=host.app_settings,
            is_streaming=host.message_runtime.is_streaming(host.current_conversation.id),
        )

        host.input_area.set_provider_model_selection(
            provider_id=getattr(host.current_conversation, 'provider_id', ''),
            model=getattr(host.current_conversation, 'model', ''),
            emit_signal=False,
        )

        try:
            mode_slug = str(getattr(host.current_conversation, 'mode', '') or '')
            host.input_area.set_mode_selection(mode_slug, apply_defaults=False)
        except Exception as e:
            logger.debug("Failed to sync mode selection in conv settings: %s", e)

        host.input_area.set_show_thinking(
            bool((host.current_conversation.settings or {}).get('show_thinking', True))
        )
        try:
            settings = host.current_conversation.settings or {}
            default_search, default_mcp = host.input_area.get_mode_default_tool_flags()
            host.input_area.set_tool_toggles(
                enable_mcp=bool(settings.get('enable_mcp', default_mcp)),
                enable_search=bool(settings.get('enable_search', default_search)),
            )
        except Exception as e:
            logger.debug("Failed to sync MCP/Search from conversation settings dialog: %s", e)

        conversations = host.services.conv_service.list_all()
        host.sidebar.update_conversations(conversations)
        host.services.app_coordinator.sync_catalog(
            providers=host.providers,
            conversation_count=len(conversations),
        )
        host.sidebar.select_conversation(host.current_conversation.id)

    def update_work_dir(self, path: str) -> None:
        host = self._host
        host.input_area.set_work_dir(path)
        conversation = self.ensure_current_conversation_shell()
        host.services.app_coordinator.apply_work_dir(conversation, path)
        self._save_current_conversation(conversation)

    def update_provider_model(self, provider_id: str, model: str) -> None:
        host = self._host
        if bool(getattr(host, 'is_syncing_input_selection', False)):
            return
        host.window_state_presenter.sync_chat_header_from_input(provider_id=provider_id, model=model)

        if not host.current_conversation:
            return

        try:
            host.services.app_coordinator.update_provider_model(
                host.current_conversation,
                providers=host.providers,
                provider_id=provider_id,
                model=model.strip() if isinstance(model, str) else host.current_conversation.model,
            )
            host.services.app_coordinator.remember_current_conversation(
                host.current_conversation,
                providers=host.providers,
                app_settings=host.app_settings,
                is_streaming=host.message_runtime.is_streaming(host.current_conversation.id),
            )
        except Exception as e:
            logger.debug("Failed to update conversation provider/model: %s", e)

        try:
            host.stats_panel.update_stats(host.current_conversation)
        except Exception as e:
            logger.debug("Failed to update stats panel: %s", e)

        try:
            host.services.conv_service.save(host.current_conversation)
        except Exception as e:
            logger.warning("Failed to save conversation: %s", e)

        host.window_state_presenter.refresh_menu_action_states()

    def update_model_ref(self, model_ref: str) -> None:
        host = self._host
        provider_name, model = split_model_ref(model_ref)
        provider_id = ""
        if provider_name:
            provider = host.services.conv_service.resolve_provider(
                host.providers,
                provider_name=provider_name,
            )
            provider_id = str(getattr(provider, "id", "") or "") if provider is not None else ""

        if not provider_id:
            provider_id = host.input_area.get_selected_provider_id()

        host.input_area.set_provider_model_selection(
            provider_id=provider_id,
            model=model or model_ref,
            emit_signal=False,
        )
        self.update_provider_model(provider_id, model or model_ref)

    def update_show_thinking(self, enabled: bool) -> None:
        self._apply_toggle('show_thinking', bool(enabled))

    def update_mcp(self, enabled: bool) -> None:
        self._apply_toggle('enable_mcp', bool(enabled))

    def update_search(self, enabled: bool) -> None:
        self._apply_toggle('enable_search', bool(enabled))

    def update_mode(self, mode_slug: str) -> None:
        host = self._host
        conversation = self.ensure_current_conversation_shell()
        host.services.app_coordinator.apply_mode(conversation, str(mode_slug or 'chat') or 'chat')
        self._save_current_conversation(conversation)

    def compact_current(self) -> None:
        host = self._host
        if not host.current_conversation:
            return
        conv = host.current_conversation
        provider = host.services.conv_service.find_provider(host.providers, conv.provider_id)
        if not provider:
            host.statusBar().showMessage("未找到对应的 Provider，无法压缩上下文", 3000)
            return
        try:
            host.services.context_service.compact(conv, provider)
            self._save_current_conversation(conv)
            host.chat_view.load_conversation(conv)
            host.stats_panel.update_stats(conv)
            host.window_state_presenter.sync_chat_header_for_current_conversation(conv.id)
            host.statusBar().showMessage("上下文已压缩", 3000)
        except Exception as e:
            host.statusBar().showMessage(f"压缩失败: {e}", 5000)

    def create_task(self, content: str) -> None:
        text = (content or "").strip()
        if not text:
            return
        self._apply_task_ops([{"action": "create", "content": text}])

    def complete_task(self, task_id: str) -> None:
        tid = (task_id or "").strip()
        if not tid:
            return
        self._apply_task_ops([{"action": "update", "id": tid, "status": "completed"}])

    def delete_task(self, task_id: str) -> None:
        tid = (task_id or "").strip()
        if not tid:
            return
        self._apply_task_ops([{"action": "delete", "id": tid}])

    # ------------------------------------------------------------------
    # Commands / export
    # ------------------------------------------------------------------

    def export_current(self, fmt: str = "markdown") -> None:
        self._command_presenter.export_current(fmt)

    def handle_command_result(self, result) -> None:
        self._command_presenter.handle_command_result(result)

    def _apply_toggle(self, key: str, value: bool) -> None:
        host = self._host
        conversation = self.ensure_current_conversation_shell()
        host.services.app_coordinator.apply_toggle(conversation, key=key, value=bool(value))
        self._save_current_conversation(conversation)

    def _apply_task_ops(self, ops: list[dict]) -> None:
        host = self._host
        if not host.current_conversation:
            return
        try:
            current_seq = host.current_conversation.next_seq_id()
            state = host.current_conversation.get_state()
            TaskService.handle_ops(state, ops, current_seq)
            state.last_updated_seq = current_seq
            host.current_conversation.set_state(state)
            host.services.conv_service.save(host.current_conversation)
        except Exception as e:
            logger.warning("Failed to apply task operations: %s", e)
            return

        try:
            host.stats_panel.update_stats(host.current_conversation)
        except Exception as e:
            logger.debug("Failed to update stats after task ops: %s", e)

    def _save_current_conversation(self, conversation: Conversation) -> None:
        host = self._host
        host.services.conv_service.save(conversation)
        host.services.app_coordinator.remember_current_conversation(
            conversation,
            providers=host.providers,
            app_settings=host.app_settings,
            is_streaming=host.message_runtime.is_streaming(conversation.id),
        )

    def _capture_selection(self, *, prefer_default_model: bool = False) -> ConversationSelection:
        host = self._host
        provider_id = str(host.input_area.get_selected_provider_id() or "").strip()
        provider = host.services.conv_service.resolve_provider(
            list(host.providers),
            provider_id=provider_id,
        )
        provider_name = str(getattr(provider, "name", "") or "").strip()
        api_type = str(getattr(provider, "api_type", "") or "").strip().lower()
        model = str(host.input_area.get_selected_model() or "").strip()

        if prefer_default_model:
            default_model_ref = str((host.app_settings or {}).get("default_chat_model", "") or "").strip()
            if default_model_ref:
                resolved_default_provider, default_model = self._resolve_default_provider_model(default_model_ref)
                if resolved_default_provider is not None:
                    provider_id = str(getattr(resolved_default_provider, "id", "") or "").strip()
                    provider_name = str(getattr(resolved_default_provider, "name", "") or "").strip()
                    api_type = str(getattr(resolved_default_provider, "api_type", "") or "").strip().lower()
                    model = self._resolve_model_for_provider(
                        resolved_default_provider,
                        requested_model=default_model,
                        fallback_model=model,
                    )

        return ConversationSelection(
            provider_id=provider_id,
            provider_name=provider_name,
            api_type=api_type,
            model=model,
            mode_slug=str(host.input_area.get_selected_mode_slug() or "chat").strip() or "chat",
            work_dir=str(host.input_area.get_work_dir() or "").strip(),
            show_thinking=bool(host.input_area.is_show_thinking_enabled()),
            enable_mcp=bool(host.input_area.is_mcp_enabled()),
            enable_search=bool(host.input_area.is_search_enabled()),
        )

    def _resolve_default_provider_model(self, model_ref: str):
        host = self._host
        provider_name, model_name = split_model_ref(model_ref)
        providers = list(host.providers)
        provider = None

        if provider_name:
            provider = host.services.conv_service.resolve_provider(
                providers,
                provider_name=provider_name,
            )

        if provider is None and model_name:
            matches = [
                candidate
                for candidate in providers
                if model_name == str(getattr(candidate, "default_model", "") or "").strip()
                or model_name in [str(item or "").strip() for item in getattr(candidate, "models", []) or []]
            ]
            if len(matches) == 1:
                provider = matches[0]

        return provider, model_name

    @staticmethod
    def _resolve_model_for_provider(provider, *, requested_model: str, fallback_model: str) -> str:
        requested = str(requested_model or "").strip()
        fallback = str(fallback_model or "").strip()
        default_model = str(getattr(provider, "default_model", "") or "").strip()
        available_models = [
            str(item or "").strip()
            for item in getattr(provider, "models", []) or []
            if str(item or "").strip()
        ]
        if requested and (not available_models or requested in available_models or requested == default_model):
            return requested
        if default_model:
            return default_model
        if available_models:
            return available_models[0]
        return fallback
