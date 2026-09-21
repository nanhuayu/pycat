"""Conversation command presenter.

Owns command-result handling, prompt invocations, and export flows so
conversation lifecycle concerns can stay focused.
"""
from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
import logging
import uuid
from typing import TYPE_CHECKING, Any, Callable

from PyQt6.QtCore import QObject, QThreadPool, pyqtSignal
from PyQt6.QtWidgets import QApplication, QFileDialog, QInputDialog

from pycat.core.content.export import CONVERSATION_FORMATS, document_format
from pycat.gui.runtime.background_job import BackgroundJob
from pycat.models.conversation import Conversation, Message

if TYPE_CHECKING:
    from pycat.core.commands import PromptInvocation
    from pycat.gui.main_window import MainWindow

logger = logging.getLogger(__name__)


class _ShellCommandSignals(QObject):
    finished = pyqtSignal(object, str, object, object)


class ConversationCommandPresenter:
    """Handles command dispatch, prompt invocations, and export."""

    def __init__(
        self,
        host: MainWindow,
        *,
        create_new_conversation: Callable[[], None],
        compact_current: Callable[[], None],
    ) -> None:
        self._host = host
        self._exports: set[BackgroundJob] = set()
        self._create_new_conversation = create_new_conversation
        self._compact_current = compact_current
        self._shell_signals = _ShellCommandSignals()
        self._shell_signals.finished.connect(self._finish_shell_command)
        self._shell_tasks = set()
        self._disposed = False

    def export_current(self, fmt: str = "markdown") -> None:
        host = self._host
        if not host.current_conversation:
            return

        self.export_conversation(host.current_conversation, fmt)

    def export_conversation(self, conversation: Conversation, fmt: str = "markdown") -> None:
        host = self._host
        if conversation is None:
            return

        try:
            fmt = document_format("", fmt)
        except ValueError as exc:
            host.chat_view.show_notice(str(exc), tone="error", conversation_id=conversation.id)
            return
        suffix, label = CONVERSATION_FORMATS[fmt]
        default_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", conversation.title or "conversation").strip(" .")
        path, _ = QFileDialog.getSaveFileName(host, "导出会话", f"{default_name}{suffix}", f"{label} (*{suffix})")
        if not path:
            return
        destination = Path(path)
        snapshot = deepcopy(conversation)
        service = host.services.conv_service
        job = BackgroundJob(lambda: service.export(snapshot, destination, format=fmt))
        self._exports.add(job)
        job.signals.finished.connect(lambda result, error: self._finish_export(job, snapshot.id, result, error))
        QThreadPool.globalInstance().start(job)

    def _finish_export(self, job, conversation_id, result, error):
        if job not in self._exports:
            return
        self._exports.discard(job)
        self._host.chat_view.show_notice(
            f"导出失败：{error}" if error else f"已导出到 {result}",
            tone="error" if error else "success", timeout_ms=5000, conversation_id=conversation_id,
        )

    def abandon_exports(self):
        self._disposed = True
        for task in tuple(self._shell_tasks):
            task.cancel()
        for job in self._exports:
            job.abandon()
        self._exports.clear()

    def handle_command_result(self, result) -> None:
        from pycat.core.commands import CommandAction, CommandResult, PromptInvocation, ShellInvocation

        if isinstance(result, str):
            self._append_info_message(result)
            return

        if not isinstance(result, CommandResult):
            return

        if result.action == CommandAction.CLEAR:
            self._create_new_conversation()
            return

        if result.action == CommandAction.COMPACT:
            self._compact_current()
            return

        if result.action == CommandAction.MODE_SWITCH:
            if result.data:
                self._switch_mode(str(result.data))
            else:
                self._open_panel('mode')
            return

        if result.action in {CommandAction.MODEL_SWITCH, CommandAction.RESUME, CommandAction.RENAME, CommandAction.OPEN_PANEL, CommandAction.EXIT}:
            try:
                if result.action == CommandAction.EXIT:
                    self._host.close()
                elif result.action == CommandAction.OPEN_PANEL:
                    self._open_panel(result.data['name'])
                else:
                    self._open_panel({CommandAction.MODEL_SWITCH: 'model', CommandAction.RESUME: 'resume', CommandAction.RENAME: 'rename'}[result.action], str(result.data or ''))
            except Exception as exc:
                self._append_info_message(str(exc))
            return

        if result.action == CommandAction.PROMPT_RUN:
            payload = result.data if isinstance(result.data, PromptInvocation) else None
            if payload is None:
                return
            self._run_prompt_invocation(payload)
            return

        if result.action == CommandAction.SHELL_RUN:
            payload = result.data if isinstance(result.data, ShellInvocation) else None
            if payload is None:
                return
            self._run_shell_command(payload)
            return

        if result.action == CommandAction.EXPORT:
            self.export_current((result.data or "markdown").strip())
            return

        if result.action == CommandAction.DISPLAY and result.display_text:
            self._append_info_message(result.display_text)

    def _open_panel(self, name, value=''):
        host = self._host
        conversation = host.current_conversation
        commands = host.services.command_service
        if name == 'resume':
            if not value:
                rows = commands.sessions()
                labels = [f"{row.get('title') or '未命名会话'} · {row['id']}" for row in rows]
                selected, ok = QInputDialog.getItem(host, '继续会话', '会话', labels, editable=False)
                if not ok or selected not in labels:
                    return
                value = rows[labels.index(selected)]['id']
            host.conversation_presenter.select(commands.resume(value).id)
            return
        if name in {'config', 'mcp', 'channels', 'agents'}:
            host.settings_presenter.open_settings(initial_page={'config': '', 'mcp': 'mcp', 'channels': 'channels', 'agents': 'modes'}[name])
            return
        if name == 'copy':
            content = next((item.content for item in reversed(conversation.messages if conversation else []) if item.role == 'assistant'), '')
            QApplication.clipboard().setText(content)
            self._append_info_message('已复制最近的回复')
            return
        if name in {'model', 'mode', 'rename', 'permissions'}:
            if conversation is None:
                conversation = host.conversation_presenter.ensure_current_conversation_shell()
                host.current_conversation = conversation
            if not host.services.conv_service.exists(conversation.id) and not host.services.conv_service.save(conversation):
                raise ValueError('无法保存会话。')
            if name == 'permissions':
                host.conversation_presenter.open_settings()
                return
            if name == 'rename':
                if not value:
                    value, ok = QInputDialog.getText(host, '重命名', '会话名称', text=conversation.title)
                    if not ok:
                        return
                host.services.conv_service.update_navigation(conversation.id, title=value)
            else:
                rows = host.services.workbench.models() if name == 'model' else [item for item in host.services.mode_catalog_service.list(conversation.work_dir) if item.is_primary_mode() and item.slug != 'channel']
                labels = [row['ref'] for row in rows] if name == 'model' else [item.slug for item in rows]
                if not value or value == 'list':
                    value, ok = QInputDialog.getItem(host, '选择模型' if name == 'model' else '选择模式', '选择', labels, editable=False)
                    if not ok:
                        return
                commands.configure(conversation.id, **{name: value})
            host.conversation_presenter.select(conversation.id)
            return
        if name in {'status', 'context', 'doctor'}:
            detail = host.services.workbench.doctor() if name == 'doctor' else {
                'session': conversation.id if conversation else '', 'model': conversation.model if conversation else '',
                'mode': conversation.mode if conversation else '', 'messages': len(conversation.messages) if conversation else 0,
                'runs': host.services.run_service.active_runs()}
            import json
            self._append_info_message(json.dumps(detail, ensure_ascii=False, indent=2))
            return
        self._append_info_message(f'打开设置中的 {name} 页面以继续。')

    def _run_shell_command(self, payload) -> None:
        host = self._host
        command = str(getattr(payload, "command", "") or "").strip()
        if not command:
            self._append_info_message("Shell 命令为空。")
            return

        if not host.current_conversation:
            try:
                host.current_conversation = host.conversation_presenter.ensure_current_conversation_shell()
            except Exception as exc:
                logger.debug("Failed to create conversation for shell invocation: %s", exc)
                self._append_info_message("无法创建会话来执行 Shell 命令。")
                return

        conversation = host.current_conversation
        if conversation is None:
            return

        if not host.services.conv_service.exists(conversation.id):
            if not host.services.conv_service.save(conversation):
                self._append_info_message("无法保存会话，Shell 未执行。")
                return

        operation_id = uuid.uuid4().hex

        async def approve(request):
            return await host.message_runtime.request_tool_approval(conversation.id, operation_id, request)

        async def execute():
            result, error = None, None
            try:
                result = await host.services.tools.run_shell(
                    command, conversation_id=conversation.id, approval_callback=approve)
            except Exception as exc:
                error = exc
            if not self._disposed:
                self._shell_signals.finished.emit(conversation, command, result, error)

        task = host.services.run_service.schedule(execute())
        self._shell_tasks.add(task)
        task.add_done_callback(self._shell_tasks.discard)

    def _finish_shell_command(self, conversation, command, result, error) -> None:
        if self._disposed:
            return
        host = self._host
        current = getattr(host, "current_conversation", None)
        if current is None or current.id != conversation.id:
            return
        if error is not None:
            host.chat_view.show_notice(f"Shell 执行失败：{error}", tone="error",
                timeout_ms=8000, conversation_id=conversation.id)
            return
        known = {message.id for message in current.messages}
        current.__dict__.update(result.conversation.clone().__dict__)
        for message in current.messages:
            if message.id not in known:
                host.chat_view.add_message(message)
        self._remember_current_conversation(current)
        refresh = getattr(getattr(host, "conversation_presenter", None), "refresh_processes", None)
        if callable(refresh):
            refresh()

    def _run_prompt_invocation(self, payload: PromptInvocation) -> None:
        host = self._host
        if not host.current_conversation:
            self._create_new_conversation()

        conversation = host.current_conversation
        if conversation is None:
            return

        mode_slug = str(getattr(payload, "mode_slug", "") or "").strip().lower()
        if mode_slug:
            self._switch_mode(mode_slug, persist=False)
            try:
                host.services.app_coordinator.apply_mode(conversation, mode_slug)
                self._remember_current_conversation(conversation)
            except Exception as exc:
                logger.debug("Failed to sync conversation mode for prompt invocation: %s", exc)

        metadata = host.services.command_service.invocation_metadata(payload)

        host.message_presenter.send(
            str(getattr(payload, "content", "") or "").strip(),
            [],
            metadata=metadata,
            delegate_profile=payload.delegate_profile,
        )

    def _append_info_message(self, content: str) -> None:
        host = self._host
        host.chat_view.show_notice(content, tone="info", timeout_ms=15000,
            conversation_id=host.current_conversation.id if host.current_conversation else "")

    def _switch_mode(self, mode_slug: str, *, persist: bool = True) -> None:
        host = self._host
        normalized = str(mode_slug or "").strip().lower()
        if not normalized:
            return

        if not host.input_area.set_mode_selection(normalized):
            self._append_info_message(f"Unknown mode: {normalized}")
            return

        conversation = host.current_conversation
        if conversation is None:
            return

        try:
            if persist:
                if not host.services.conv_service.exists(conversation.id):
                    host.services.conv_service.save(conversation)
                updated = host.services.command_service.configure(conversation.id, mode=normalized)
                conversation.__dict__.update(updated.__dict__)
            else:
                host.services.app_coordinator.apply_mode(conversation, normalized)
            self._remember_current_conversation(conversation)
        except Exception as exc:
            host.input_area.set_mode_selection(conversation.mode)
            self._append_info_message(str(exc))

    def _remember_current_conversation(self, conversation: Conversation) -> None:
        host = self._host
        host.services.app_coordinator.remember_current_conversation(
            conversation,
            providers=host.providers,
            app_settings=host.app_settings,
            is_streaming=host.message_runtime.is_streaming(conversation.id),
        )
