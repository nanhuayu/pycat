"""Conversation lifecycle presenter.

Extracts conversation CRUD + selection logic from MainWindow,
reducing it by ~170 lines.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from typing import TYPE_CHECKING

from PyQt6.QtCore import QThreadPool
from PyQt6.QtWidgets import QMessageBox

from core.agent.events.conversation import conversation_patch_payload
from core.app.state import ConversationSelection
from core.llm.model_selection import select_default_provider_model
from core.memory.service import MemoryService
from core.state.todo import TodoService
from gui.dialogs.conversation_settings_dialog import ConversationSettingsDialog
from gui.presenters.conversation_command_presenter import ConversationCommandPresenter
from gui.runtime.background_job import BackgroundJob
from models.contracts.config import AppConfig
from models.contracts.tooling import (
    ToolPermissionConfig,
    normalize_permission_preset,
    permission_config_for_preset,
)
from models.conversation import Conversation
from models.model_ref import split_model_ref
from models.streaming import ConversationPatch

if TYPE_CHECKING:
    from gui.main_window import MainWindow

logger = logging.getLogger(__name__)


class ConversationPresenter:
    """Handles conversation selection, creation, import, and deletion."""

    def __init__(self, host: MainWindow) -> None:
        self._host = host
        self._settings_dialog: ConversationSettingsDialog | None = None
        self._compact_jobs: dict[str, BackgroundJob] = {}
        self._workspace_jobs: dict[str, BackgroundJob] = {}
        self._delete_jobs: dict[str, BackgroundJob] = {}
        self._command_presenter = ConversationCommandPresenter(
            host,
            create_new_conversation=self.new,
            compact_current=self.compact_current,
        )

    def is_compacting(self, conversation_id: str) -> bool:
        return str(conversation_id or "") in self._compact_jobs

    def active_operation(self, conversation_id: str) -> str:
        """Return the current GUI or shared lifecycle operation."""
        key = str(conversation_id or "").strip()
        if not key:
            return ""
        if key in self._delete_jobs:
            return "delete"
        if key in self._workspace_jobs:
            return "workspace"
        if key in self._compact_jobs:
            return "compact"
        lookup = getattr(getattr(self._host.services, "conv_service", None), "active_operation", None)
        return str(lookup(key) or "") if callable(lookup) else ""

    def is_conversation_active(self, conversation_id: str) -> bool:
        """Return the shared runtime activity fact for this conversation."""
        key = str(conversation_id or "").strip()
        if not key:
            return False
        checker = getattr(getattr(self._host.services, "conv_service", None), "is_active", None)
        if callable(checker) and checker(key):
            return True
        runtime = getattr(self._host, "message_runtime", None)
        is_streaming = getattr(runtime, "is_streaming", None)
        return bool(callable(is_streaming) and is_streaming(key))

    def is_maintaining(self, conversation_id: str) -> bool:
        """Return whether a conversation has a background lifecycle operation."""
        conversation_key = str(conversation_id or "")
        local_active = bool(
            conversation_key
            and (
                conversation_key in self._compact_jobs
                or conversation_key in self._workspace_jobs
                or conversation_key in self._delete_jobs
            )
        )
        checker = getattr(
            getattr(self._host.services, "conv_service", None),
            "is_lifecycle_active",
            None,
        )
        shared_active = bool(callable(checker) and checker(conversation_key))
        return local_active or shared_active

    def abandon_background_jobs(self) -> None:
        """Prevent late GUI callbacks while the application is closing."""
        jobs = [
            *self._compact_jobs.values(),
            *self._workspace_jobs.values(),
            *self._delete_jobs.values(),
        ]
        self._compact_jobs.clear()
        self._workspace_jobs.clear()
        self._delete_jobs.clear()
        for job in jobs:
            try:
                job.abandon()
            except Exception as exc:
                logger.debug("Failed to abandon conversation job: %s", exc)

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

    @staticmethod
    def _sync_permission_preset_to_input(host, conversation: Conversation) -> None:
        setter = getattr(host.input_area, "set_permission_preset", None)
        if not callable(setter):
            return
        preset = str((conversation.settings or {}).get("permission_preset", "default") or "default")
        setter(preset)

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def select(self, conversation_id: str) -> None:
        host = self._host
        previous = host.current_conversation
        if previous is not None and str(getattr(previous, "id", "") or "") != str(conversation_id or ""):
            previous_state = host.message_runtime.get_state(previous.id)
            if previous_state is not None:
                host.message_runtime.reject_pending_approvals(
                    request_id=str(getattr(previous_state, "request_id", "") or "")
                )
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
            show_thinking_default = bool(host.app_settings.get("show_thinking", True))
            show_thinking = bool(
                (conversation.settings or {}).get("show_thinking", show_thinking_default)
            )
            host.current_conversation = conversation
            set_show_thinking = getattr(host.chat_view, "set_show_thinking", None)
            if callable(set_show_thinking):
                set_show_thinking(show_thinking, refresh=False)
            host.chat_view.load_conversation(conversation)
            host.inspector_panel.update_stats(conversation)
            self.refresh_processes()

            # Sync per-conversation toggles
            host.input_area.set_show_thinking(show_thinking)
            self._sync_permission_preset_to_input(host, conversation)

            host.input_area.set_model_ref(
                host.services.conv_service.build_model_ref(conversation, host.providers)
            )

            # Sync mode selection
            try:
                mode_slug = str(getattr(conversation, "mode", "") or "")
                host.input_area.set_mode_selection(mode_slug)
            except Exception as e:
                logger.debug("Failed to sync mode selection during select: %s", e)

            # Update chat header
            token_snapshot = None
            try:
                from core.llm.token_budget import build_token_usage_snapshot

                token_snapshot = build_token_usage_snapshot(
                    conversation,
                    providers=host.providers,
                    provider_id=str(getattr(conversation, "provider_id", "") or ""),
                    provider_name=str(getattr(conversation, "provider_name", "") or ""),
                    model_id=str(getattr(conversation, "model", "") or ""),
                    compact_threshold_ratio=AppConfig.from_dict(
                        host.app_settings or {}
                    ).context.compression_policy.token_threshold_ratio,
                )
            except Exception as e:
                logger.debug("Failed to build token snapshot during select: %s", e)
            host.chat_view.update_header(
                host.services.conv_service.build_model_ref(conversation, host.providers),
                msg_count=len(conversation.messages),
            )
            set_snapshot = getattr(host.input_area, "set_token_snapshot", None)
            if callable(set_snapshot):
                set_snapshot(token_snapshot)
            work_dir = getattr(conversation, "work_dir", "")
            host.chat_view.update_work_dir(work_dir)
            host.input_area.set_work_dir(work_dir)
            host.input_area.set_conversation(conversation)
            restore_guidance = getattr(
                getattr(host, "message_presenter", None),
                "restore_recovered_guidance",
                None,
            )
            if callable(restore_guidance):
                restore_guidance(conversation.id)

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
        selection = self._capture_selection(prefer_app_default=True)
        host.current_conversation = host.services.app_coordinator.create_conversation(selection)
        try:
            host.input_area.set_model_ref(
                host.services.conv_service.build_model_ref(host.current_conversation, host.providers)
            )
        except Exception as e:
            logger.debug("Failed to sync provider/model selection for new conversation: %s", e)
        try:
            mode_slug = str(getattr(host.current_conversation, "mode", "") or "chat").strip() or "chat"
            host.input_area.set_mode_selection(mode_slug)
        except Exception as e:
            logger.debug("Failed to sync mode selection for new conversation: %s", e)
        self._sync_permission_preset_to_input(host, host.current_conversation)
        host.services.app_coordinator.remember_current_conversation(
            host.current_conversation,
            providers=host.providers,
            app_settings=host.app_settings,
            is_streaming=False,
        )
        host.chat_view.load_conversation(host.current_conversation)
        host.inspector_panel.update_stats(host.current_conversation)
        self.refresh_processes()
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
            host.sidebar.update_conversations(conversations)
            host.services.app_coordinator.sync_catalog(
                providers=host.providers,
                conversation_count=len(conversations),
            )
            if hasattr(host.sidebar, "select_conversation"):
                host.sidebar.select_conversation(host.current_conversation.id)
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
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            return
        if self.is_maintaining(conversation_id):
            self._show_notice(
                host,
                "该会话正在处理，请稍候",
                tone="warning",
                conversation_id=conversation_id,
            )
            return
        if self.is_conversation_active(conversation_id):
            self._show_notice(
                host,
                "请等待当前任务结束后再删除会话",
                tone="warning",
                conversation_id=conversation_id,
            )
            return

        def operation():
            closed = asyncio.run(host.services.tool_manager.close_conversation_sessions(conversation_id))
            if closed is False:
                return False, host.services.conv_service.list_all()
            deleted = bool(host.services.conv_service.delete(conversation_id))
            return deleted, host.services.conv_service.list_all()

        job = BackgroundJob(operation)
        self._delete_jobs[conversation_id] = job
        job.signals.finished.connect(
            lambda result, error, cid=conversation_id, active_job=job: self._finish_delete(
                cid,
                active_job,
                result,
                error,
            )
        )
        self._sync_lifecycle_actions(conversation_id)
        QThreadPool.globalInstance().start(job)

    def _finish_delete(self, conversation_id: str, job: BackgroundJob, result, error) -> None:
        host = self._host
        if self._delete_jobs.get(conversation_id) is not job:
            return
        self._delete_jobs.pop(conversation_id, None)
        if error is not None:
            self._show_notice(
                host,
                f"删除会话失败：{error}",
                5000,
                tone="error",
                conversation_id=conversation_id,
            )
            self._sync_lifecycle_actions(conversation_id)
            return
        deleted = result[0] if isinstance(result, tuple) and len(result) == 2 else result
        conversations = result[1] if isinstance(result, tuple) and len(result) == 2 else []
        if not bool(deleted):
            self._show_notice(
                host,
                "删除会话失败：会话不存在或无法删除",
                5000,
                tone="error",
                conversation_id=conversation_id,
            )
            self._sync_lifecycle_actions(conversation_id)
            return

        host.sidebar.update_conversations(conversations)
        host.services.app_coordinator.sync_catalog(
            providers=host.providers,
            conversation_count=len(conversations),
        )
        deleted_current = bool(
            host.current_conversation and host.current_conversation.id == conversation_id
        )
        if deleted_current:
            host.current_conversation = None
            host.services.app_coordinator.clear_current_conversation()
            host.chat_view.clear()
            host.inspector_panel.update_stats(None)
            self.refresh_processes()
            host.window_state_presenter.sync_chat_header_from_input()
            host.chat_view.update_work_dir("")
            host.input_area.set_work_dir("")
            host.input_area.set_conversation(None)
            host.window_state_presenter.sync_input_enabled()
        self._show_notice(
            host,
            "会话已删除",
            3000,
            tone="success",
            conversation_id=None if deleted_current else conversation_id,
        )
        self._sync_lifecycle_actions(conversation_id)

    def _sync_lifecycle_actions(self, conversation_id: str) -> None:
        presenter = getattr(self._host, "window_state_presenter", None)
        if presenter is None:
            return
        if str(getattr(self._host.current_conversation, "id", "") or "") == str(conversation_id or ""):
            presenter.sync_input_enabled()
        else:
            presenter.refresh_menu_action_states()

    @staticmethod
    def _show_notice(
        host,
        text: str,
        timeout: int = 3000,
        *,
        tone: str = "info",
        conversation_id: str | None = None,
    ) -> None:
        chat_view = getattr(host, "chat_view", None)
        show_notice = getattr(chat_view, "show_header_notice", None)
        if callable(show_notice):
            show_notice(
                text,
                tone=tone,
                timeout_ms=timeout,
                conversation_id=conversation_id,
            )

    @staticmethod
    def _restore_work_dir_display(host, path: str) -> None:
        chat_view = getattr(host, "chat_view", None)
        if chat_view is not None and callable(getattr(chat_view, "update_work_dir", None)):
            chat_view.update_work_dir(path)
        input_area = getattr(host, "input_area", None)
        if input_area is not None and callable(getattr(input_area, "set_work_dir", None)):
            input_area.set_work_dir(path)

    def delete_current(self) -> None:
        host = self._host
        if not host.current_conversation:
            return
        self.delete(host.current_conversation.id)

    def open_settings(self) -> None:
        host = self._host
        raise_approval = getattr(getattr(host, "message_presenter", None), "raise_pending_approval", None)
        if callable(raise_approval) and raise_approval():
            return
        if self._settings_dialog is not None:
            self._settings_dialog.show()
            self._settings_dialog.raise_()
            self._settings_dialog.activateWindow()
            return
        if not host.current_conversation:
            host.current_conversation = self.ensure_current_conversation_shell()

        conversation = host.current_conversation
        dlg = ConversationSettingsDialog(
            conversation,
            providers=host.providers,
            default_show_thinking=bool(host.app_settings.get('show_thinking', True)),
            parent=host,
        )
        self._settings_dialog = dlg
        model_edit_signal = getattr(dlg, "model_edit_requested", None)
        edit_model_ref = getattr(getattr(host, "settings_presenter", None), "edit_model_ref", None)
        if model_edit_signal is not None and callable(edit_model_ref):
            model_edit_signal.connect(edit_model_ref)
        dlg.accepted.connect(
            lambda dlg=dlg, conversation=conversation: self._apply_conversation_settings(
                dlg, conversation
            )
        )
        dlg.finished.connect(lambda _result, dlg=dlg: self._release_settings_dialog(dlg))
        dlg.open()
        dlg.raise_()
        dlg.activateWindow()

    def _release_settings_dialog(self, dialog: ConversationSettingsDialog) -> None:
        if self._settings_dialog is dialog:
            self._settings_dialog = None
        dialog.deleteLater()

    def _apply_conversation_settings(
        self,
        dlg: ConversationSettingsDialog,
        conversation: Conversation,
    ) -> None:
        host = self._host
        update = dlg.build_update()
        host.services.app_coordinator.apply_settings_update(
            conversation,
            update=update,
            providers=host.providers,
        )
        host.services.conv_service.save(conversation)
        if host.current_conversation is not conversation:
            return
        host.inspector_panel.update_stats(conversation)
        host.services.app_coordinator.remember_current_conversation(
            conversation,
            providers=host.providers,
            app_settings=host.app_settings,
            is_streaming=host.message_runtime.is_streaming(conversation.id),
        )

        host.input_area.set_model_ref(
            host.services.conv_service.build_model_ref(host.current_conversation, host.providers)
        )

        try:
            mode_slug = str(getattr(host.current_conversation, 'mode', '') or '')
            host.input_area.set_mode_selection(mode_slug)
        except Exception as e:
            logger.debug("Failed to sync mode selection in conv settings: %s", e)

        host.input_area.set_show_thinking(
            bool((host.current_conversation.settings or {}).get('show_thinking', True))
        )
        self._sync_permission_preset_to_input(host, host.current_conversation)
        conversations = host.services.conv_service.list_all()
        host.sidebar.update_conversations(conversations)
        host.services.app_coordinator.sync_catalog(
            providers=host.providers,
            conversation_count=len(conversations),
        )
        host.sidebar.select_conversation(host.current_conversation.id)

    def update_work_dir(self, path: str) -> None:
        host = self._host
        if host.current_conversation is None:
            conversation = self.ensure_current_conversation_shell()
            host.input_area.set_work_dir(path)
            self._save_current_conversation(conversation)
            return
        conversation = host.current_conversation
        conversation_id = str(getattr(conversation, "id", "") or "")
        previous = str(getattr(conversation, "work_dir", "") or "")
        if self.is_maintaining(conversation_id):
            self._restore_work_dir_display(host, previous)
            self._show_notice(
                host,
                "该会话正在处理，请稍候再切换工作区",
                tone="warning",
                conversation_id=conversation_id,
            )
            return
        if self.is_conversation_active(conversation_id):
            self._restore_work_dir_display(host, previous)
            self._show_notice(
                host,
                "请等待当前任务结束后再切换工作区",
                tone="warning",
                conversation_id=conversation_id,
            )
            return

        snapshot = Conversation.from_dict(conversation.to_dict())

        def operation():
            result = host.services.app_coordinator.apply_work_dir(snapshot, path)
            return result, host.services.conv_service.list_all()

        job = BackgroundJob(operation)
        self._workspace_jobs[conversation_id] = job
        job.signals.finished.connect(
            lambda result, error, cid=conversation_id, active_job=job, old=previous: self._finish_work_dir(
                cid,
                active_job,
                old,
                result,
                error,
            )
        )
        self._restore_work_dir_display(host, previous)
        self._sync_lifecycle_actions(conversation_id)
        QThreadPool.globalInstance().start(job)

    def _finish_work_dir(
        self,
        conversation_id: str,
        job: BackgroundJob,
        previous: str,
        result,
        error,
    ) -> None:
        host = self._host
        if self._workspace_jobs.get(conversation_id) is not job:
            return
        self._workspace_jobs.pop(conversation_id, None)
        current_id = str(getattr(host.current_conversation, "id", "") or "")
        if error is not None:
            if current_id == conversation_id:
                self._restore_work_dir_display(host, previous)
            self._show_notice(
                host,
                f"切换工作区失败：{error}",
                5000,
                tone="error",
                conversation_id=conversation_id,
            )
            self._sync_lifecycle_actions(conversation_id)
            return
        migration_result = result[0] if isinstance(result, tuple) and len(result) == 2 else result
        conversations = result[1] if isinstance(result, tuple) and len(result) == 2 else []
        if not getattr(migration_result, "ok", False):
            if current_id == conversation_id:
                self._restore_work_dir_display(host, previous)
            self._show_notice(
                host,
                f"切换工作区失败：{getattr(migration_result, 'error', '') or '无法迁移会话文件'}",
                5000,
                tone="error",
                conversation_id=conversation_id,
            )
            self._sync_lifecycle_actions(conversation_id)
            return

        if current_id == conversation_id:
            migrated = host.services.conv_service.load(conversation_id)
            if migrated is None:
                self._show_notice(
                    host,
                    "切换工作区失败：迁移后无法读取会话",
                    5000,
                    tone="error",
                    conversation_id=conversation_id,
                )
                self._sync_lifecycle_actions(conversation_id)
                return
            host.current_conversation = migrated
            host.input_area.set_conversation(migrated)
            host.chat_view.update_work_dir(migrated.work_dir)
            host.input_area.set_work_dir(migrated.work_dir)
            host.inspector_panel.update_stats(migrated)
            host.services.app_coordinator.remember_current_conversation(
                migrated,
                providers=host.providers,
                app_settings=host.app_settings,
                is_streaming=False,
            )
        host.sidebar.update_conversations(conversations)
        host.services.app_coordinator.sync_catalog(
            providers=host.providers,
            conversation_count=len(conversations),
        )
        self._show_notice(
            host,
            "工作区已切换",
            3000,
            tone="success",
            conversation_id=conversation_id,
        )
        self._sync_lifecycle_actions(conversation_id)

    def refresh_processes(self) -> None:
        host = self._host
        update = getattr(host.inspector_panel, "update_processes", None)
        if not callable(update):
            return
        conversation = host.current_conversation
        list_processes = getattr(getattr(host.services, "tool_manager", None), "list_processes", None)
        if conversation is None or not callable(list_processes):
            update([])
            return
        update(list_processes(str(getattr(conversation, "id", "") or "")))

    def stop_process(self, process_id: str) -> None:
        host = self._host
        conversation = host.current_conversation
        if conversation is None:
            return
        host.services.tool_manager.stop_process(
            str(process_id or ""),
            conversation_id=str(getattr(conversation, "id", "") or ""),
        )
        self.refresh_processes()

    def stop_all_processes(self) -> None:
        host = self._host
        conversation = host.current_conversation
        if conversation is None:
            return
        host.services.tool_manager.stop_all_processes(str(getattr(conversation, "id", "") or ""))
        self.refresh_processes()

    def update_provider_model(self, provider_id: str, model: str) -> None:
        host = self._host
        if bool(getattr(host, 'is_syncing_input_selection', False)):
            return
        if not host.current_conversation:
            return
        if self.is_conversation_active(host.current_conversation.id):
            return
        host.window_state_presenter.sync_chat_header_from_input(provider_id=provider_id, model=model)

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
            host.inspector_panel.update_stats(host.current_conversation)
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
        self.update_provider_model(provider_id, model or model_ref)

    def update_mode(self, mode_slug: str) -> None:
        host = self._host
        if host.current_conversation and self.is_conversation_active(host.current_conversation.id):
            return
        conversation = self.ensure_current_conversation_shell()
        host.services.app_coordinator.apply_mode(conversation, str(mode_slug or 'chat') or 'chat')
        self._save_current_conversation(conversation)

    def update_permission_preset(
        self,
        preset: str,
        *,
        confirm_allow: bool = True,
        conversation_id: str = "",
    ) -> bool:
        host = self._host
        normalized = str(preset or "").strip() or "default"
        if normalized not in ("default", "ask", "deny", "allow", "custom"):
            return False
        target_id = str(conversation_id or "").strip()
        current = host.current_conversation
        current_id = str(getattr(current, "id", "") or "").strip()
        if target_id and target_id != current_id:
            conversation = host.services.conv_service.load(target_id)
            if conversation is None:
                return False
        elif current is not None:
            conversation = current
        else:
            conversation = self.ensure_current_conversation_shell()
        is_current = str(getattr(conversation, "id", "") or "").strip() == str(
            getattr(host.current_conversation, "id", "") or ""
        ).strip()
        if self.is_maintaining(conversation.id):
            if is_current:
                self._sync_permission_preset_to_input(host, conversation)
            return False
        previous_settings = dict(conversation.settings or {})
        previous = str((conversation.settings or {}).get("permission_preset", "default") or "default")
        if normalized == previous:
            if is_current:
                self._sync_permission_preset_to_input(host, conversation)
            return True
        if normalized == "allow" and confirm_allow:
            answer = QMessageBox.warning(
                host,
                "确认全部放行",
                "全部放行将跳过本会话所有工具确认（含高风险命令执行与文件修改），"
                "适合完全可信的任务；无人值守场景请谨慎。\n\n是否继续？",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Ok:
                if is_current:
                    self._sync_permission_preset_to_input(host, conversation)
                return False
        host.services.conv_service.set_setting(conversation, "permission_preset", normalized)
        if not host.services.conv_service.save(conversation):
            conversation.settings = previous_settings
            if is_current:
                self._sync_permission_preset_to_input(host, conversation)
            return False
        if host.message_runtime.is_streaming(conversation.id):
            custom = ToolPermissionConfig.from_settings_dict(host.app_settings)
            permissions = permission_config_for_preset(normalized, custom=custom)
            revision = host.message_runtime.update_permissions(conversation.id, permissions)
            if revision is None and host.message_runtime.is_streaming(conversation.id):
                conversation.settings = previous_settings
                host.services.conv_service.save(conversation)
                if is_current:
                    self._sync_permission_preset_to_input(host, conversation)
                return False
        if is_current:
            host.services.app_coordinator.remember_current_conversation(
                conversation,
                providers=host.providers,
                app_settings=host.app_settings,
                is_streaming=host.message_runtime.is_streaming(conversation.id),
            )
            self._sync_permission_preset_to_input(host, conversation)
        return True

    def compact_current(self) -> None:
        host = self._host
        if not host.current_conversation:
            return
        conv = host.current_conversation
        conversation_id = str(conv.id or "")
        if self.is_compacting(conversation_id):
            host.chat_view.show_header_notice(
                "该会话正在压缩",
                tone="warning",
                timeout_ms=3000,
                conversation_id=conversation_id,
            )
            return
        if self.is_maintaining(conversation_id):
            host.chat_view.show_header_notice(
                "该会话正在处理，请稍候",
                tone="warning",
                timeout_ms=3000,
                conversation_id=conversation_id,
            )
            return
        if self.is_conversation_active(conversation_id):
            host.chat_view.show_header_notice(
                "请等待当前任务结束后再压缩上下文",
                tone="warning",
                timeout_ms=3000,
                conversation_id=conversation_id,
            )
            return
        provider = host.services.conv_service.find_provider(host.providers, conv.provider_id)
        if not provider:
            host.chat_view.show_header_notice(
                "未找到对应的 Provider，无法压缩上下文",
                tone="error",
                timeout_ms=5000,
                conversation_id=conversation_id,
            )
            return
        snapshot = Conversation.from_dict(conv.to_dict())
        source_fingerprint = self._conversation_fingerprint(conv)
        request_id = f"compact-{uuid.uuid4()}"

        def operation():
            report = asyncio.run(host.services.context_service.compact_async(snapshot, provider))
            return report, snapshot

        job = BackgroundJob(operation)
        self._compact_jobs[conversation_id] = job
        job.signals.finished.connect(
            lambda result, error, cid=conversation_id, rid=request_id, fingerprint=source_fingerprint, active_job=job: self._finish_compact(
                cid,
                rid,
                fingerprint,
                active_job,
                result,
                error,
            )
        )
        self._sync_compact_busy(conversation_id)
        QThreadPool.globalInstance().start(job)

    def _finish_compact(
        self,
        conversation_id: str,
        request_id: str,
        source_fingerprint: str,
        job: BackgroundJob,
        result,
        error,
    ) -> None:
        host = self._host
        if self._compact_jobs.get(conversation_id) is not job:
            return
        self._compact_jobs.pop(conversation_id, None)
        self._sync_compact_busy(conversation_id)
        if error is not None:
            host.chat_view.show_header_notice(
                f"压缩失败：{error}",
                tone="error",
                timeout_ms=8000,
                conversation_id=conversation_id,
            )
            return
        report, snapshot = result
        current = host.current_conversation
        is_current = current is not None and current.id == conversation_id
        exists = getattr(host.services.conv_service, "exists", None)
        persisted_exists = bool(exists(conversation_id)) if callable(exists) else True
        if not persisted_exists:
            host.chat_view.show_header_notice(
                "会话已被删除，本次压缩结果已丢弃",
                tone="warning",
                timeout_ms=5000,
                conversation_id=conversation_id,
            )
            return
        target = current if is_current else host.services.conv_service.load(conversation_id)
        if target is None:
            host.chat_view.show_header_notice(
                "会话已被删除，本次压缩结果已丢弃",
                tone="warning",
                timeout_ms=5000,
                conversation_id=conversation_id,
            )
            return
        if self._conversation_fingerprint(target) != source_fingerprint:
            host.chat_view.show_header_notice(
                "会话已发生变化，本次压缩结果未写回",
                tone="warning",
                timeout_ms=5000,
                conversation_id=conversation_id,
            )
            return
        payload = conversation_patch_payload(snapshot).get("conversation_patch") or {}
        patch = ConversationPatch.from_payload(payload, conversation_id=conversation_id)
        if patch is not None:
            host.message_presenter.on_conversation_patch(conversation_id, request_id, patch)
        host.chat_view.show_header_notice(
            self._compact_result_text(report),
            tone="success",
            timeout_ms=5000,
            conversation_id=conversation_id,
        )

    def _sync_compact_busy(self, conversation_id: str) -> None:
        host = self._host
        current_id = str(getattr(host.current_conversation, "id", "") or "")
        if current_id == str(conversation_id or ""):
            setter = getattr(host.input_area, "set_context_busy", None)
            if callable(setter):
                setter(self.is_compacting(current_id))
            set_mutations_enabled = getattr(host.inspector_panel, "set_mutations_enabled", None)
            if callable(set_mutations_enabled):
                set_mutations_enabled(
                    not self.is_compacting(current_id) and not host.message_runtime.is_streaming(current_id)
                )
        host.window_state_presenter.refresh_menu_action_states()
        host.window_state_presenter.sync_runtime_state(conversation_id)

    @staticmethod
    def _conversation_fingerprint(conversation: Conversation) -> str:
        payload = conversation.to_dict()
        payload.pop("updated_at", None)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8", errors="replace")).hexdigest()

    @staticmethod
    def _compact_result_text(report) -> str:
        archived = int(getattr(report, "archived_messages", 0) or 0)
        if archived > 0 and bool(getattr(report, "summary_updated", False)):
            return f"已压缩 {archived} 条历史消息"
        reason = str(getattr(report, "reason", "") or "")
        if reason == "maintenance_in_progress":
            return "该会话正在压缩"
        if reason in {"no_candidates", "up_to_date", "below_threshold"}:
            return "当前没有可压缩的历史"
        metrics = dict(getattr(report, "metrics", {}) or {})
        if metrics.get("fallback_reason") or metrics.get("skip_reason"):
            return "压缩未产生有效节省，历史保持不变"
        return "当前没有可压缩的历史"

    def create_task(self, content: str) -> None:
        text = (content or "").strip()
        if not text:
            return
        self._apply_task_ops([{"action": "create", "title": text}])

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

    def promote_memory_candidate(self, candidate_id: str) -> None:
        self._review_memory_candidate(candidate_id, promote=True)

    def reject_memory_candidate(self, candidate_id: str) -> None:
        self._review_memory_candidate(candidate_id, promote=False)

    def _review_memory_candidate(self, candidate_id: str, *, promote: bool) -> None:
        host = self._host
        conversation = host.current_conversation
        target = str(candidate_id or "").strip()
        if conversation is None or not target:
            return
        if self.is_conversation_active(conversation.id):
            return
        try:
            current_seq = conversation.next_seq_id()
            state = conversation.get_state()
            if promote:
                result = MemoryService.promote_memory_candidate(
                    state,
                    candidate_id=target,
                    work_dir=conversation.work_dir,
                    current_seq=current_seq,
                )
            else:
                result = MemoryService.reject_memory_candidate(
                    state,
                    candidate_id=target,
                    current_seq=current_seq,
                )
            if "not found" in result.lower():
                raise ValueError(result)
            conversation.set_state(state)
            host.services.conv_service.save(conversation)
            host.inspector_panel.update_stats(conversation)
            host.chat_view.show_header_notice(
                result,
                tone="success",
                timeout_ms=3000,
                conversation_id=conversation.id,
            )
        except Exception as exc:
            logger.warning("Failed to review memory candidate %s: %s", target, exc)
            host.chat_view.show_header_notice(
                f"记忆候选操作失败：{exc}",
                tone="error",
                timeout_ms=5000,
                conversation_id=conversation.id,
            )

    # ------------------------------------------------------------------
    # Commands / export
    # ------------------------------------------------------------------

    def export_current(self, fmt: str = "markdown") -> None:
        self._command_presenter.export_current(fmt)

    def export(self, conversation_id: str, fmt: str = "markdown") -> None:
        host = self._host
        conversation = host.services.conv_service.load(str(conversation_id or ""))
        if conversation is None:
            QMessageBox.warning(host, "导出失败", "未找到要导出的会话")
            return
        self._command_presenter.export_conversation(conversation, fmt)

    def handle_command_result(self, result) -> None:
        self._command_presenter.handle_command_result(result)

    def _apply_toggle(self, key: str, value: bool) -> None:
        host = self._host
        if host.current_conversation and self.is_conversation_active(host.current_conversation.id):
            return
        conversation = self.ensure_current_conversation_shell()
        host.services.app_coordinator.apply_toggle(conversation, key=key, value=bool(value))
        self._save_current_conversation(conversation)

    def _apply_task_ops(self, ops: list[dict]) -> None:
        host = self._host
        if not host.current_conversation:
            return
        if self.is_conversation_active(host.current_conversation.id):
            return
        try:
            current_seq = host.current_conversation.next_seq_id()
            state = host.current_conversation.get_state()
            TodoService.handle_ops(state, ops, current_seq)
            state.last_updated_seq = current_seq
            host.current_conversation.set_state(state)
            host.services.conv_service.save(host.current_conversation)
        except Exception as e:
            logger.warning("Failed to apply task operations: %s", e)
            return

        try:
            host.inspector_panel.update_stats(host.current_conversation)
        except Exception as e:
            logger.debug("Failed to update stats after task ops: %s", e)

    def _save_current_conversation(self, conversation: Conversation) -> None:
        host = self._host
        if not host.services.conv_service.save(conversation):
            logger.debug("Conversation save rejected for %s", getattr(conversation, "id", ""))
            return
        host.services.app_coordinator.remember_current_conversation(
            conversation,
            providers=host.providers,
            app_settings=host.app_settings,
            is_streaming=host.message_runtime.is_streaming(conversation.id),
        )

    def _capture_selection(self, *, prefer_app_default: bool = False) -> ConversationSelection:
        host = self._host
        provider_id = str(host.input_area.get_selected_provider_id() or "").strip()
        provider = host.services.conv_service.resolve_provider(
            list(host.providers),
            provider_id=provider_id,
        )
        provider_name = str(getattr(provider, "name", "") or "").strip()
        api_type = str(getattr(provider, "api_type", "") or "").strip().lower()
        model = str(host.input_area.get_selected_model() or "").strip()

        if prefer_app_default:
            default_model_ref = str((host.app_settings or {}).get("default_chat_model", "") or "").strip()
            if default_model_ref:
                selection = select_default_provider_model(
                    host.providers,
                    default_model_ref=default_model_ref,
                )
                if selection.provider is not None:
                    provider_id = selection.provider_id
                    provider_name = selection.provider_name
                    api_type = selection.api_type
                    model = selection.model

        return ConversationSelection(
            provider_id=provider_id,
            provider_name=provider_name,
            api_type=api_type,
            model=model,
            mode_slug=str(host.input_area.get_selected_mode_slug() or "chat").strip() or "chat",
            work_dir=str(host.input_area.get_work_dir() or "").strip(),
            show_thinking=bool(host.input_area.is_show_thinking_enabled()),
            permission_preset=normalize_permission_preset(
                host.input_area.get_permission_preset()
            ),
        )

