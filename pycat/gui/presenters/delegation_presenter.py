"""Qt adapter for the existing application handoff and conversation views."""
from __future__ import annotations

import uuid

from PyQt6.QtCore import QObject, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtWidgets import QInputDialog

from pycat.gui.runtime.background_job import BackgroundJob
from pycat.gui.view_models.delegated_tasks import independent_task_views, subtask_views
from pycat.models.contracts.agent import RunRequest


class DelegationPresenter(QObject):
    completed = pyqtSignal(str, object, object, str, str)

    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.service = host.services.delegation_service
        self._job = None
        self._refresh_again = False
        self._disposed = False
        self._content_revision = -1
        self._submissions = {}
        self.completed.connect(self._finished)
        host.inspector_panel.delegated_tasks.operation_requested.connect(self.operate)
        host.chat_view.conversation_changed.connect(self.refresh)
        host.message_runtime.runtime_event.connect(self.on_runtime_event)
        host.message_runtime.run_finished.connect(self.refresh_subtasks)
        host.destroyed.connect(self.abandon)

    def on_app_state(self, state):
        if state.content_revision != self._content_revision and 'delegation' in state.changed_domains:
            self._content_revision = state.content_revision
            self.refresh()

    def refresh(self):
        if self._disposed:
            return
        current_id = self.host.chat_view.conversation_id
        self.host.inspector_panel.delegated_tasks.set_conversation(current_id)
        self.refresh_subtasks(current_id)
        if self._job is not None:
            self._refresh_again = True
            return
        if not current_id:
            return
        service = self.service
        job = BackgroundJob(lambda: (service.list(current_id), service.conversations.list_all()))
        self._job = job
        job.signals.finished.connect(lambda result, error: self._project(job, current_id, result, error))
        QThreadPool.globalInstance().start(job)

    def _project(self, job, current_id, result, error):
        if self._disposed or self._job is not job:
            return
        self._job = None
        if error is None and self.host.chat_view.conversation_id == current_id:
            cards, rows = result
            self.host.inspector_panel.delegated_tasks.set_tasks(
                independent_task_views(cards), kind='independent', conversation_id=current_id)
            self.host.sidebar.update_conversations(rows)
            own = next((card for card in cards if card['id'] == current_id), None)
            if own and own['result_message_id'] and not self.host.message_runtime.is_streaming(current_id):
                displayed = self.host.current_conversation
                if displayed is not None and not any(m.id == own['result_message_id'] for m in displayed.messages):
                    self.host.conversation_presenter.select(current_id)
        if self._refresh_again:
            self._refresh_again = False
            self.refresh()

    def refresh_subtasks(self, conversation_id, *_args):
        if self._disposed or self.host.chat_view.conversation_id != conversation_id:
            return
        runtime = self.host.message_runtime
        state = runtime.get_state(conversation_id)
        conversation = state.conversation if state is not None else self.host.current_conversation
        tasks = subtask_views(conversation, request_id=state.request_id if state else '') if conversation else []
        self.host.inspector_panel.delegated_tasks.set_tasks(tasks, kind='subagent', conversation_id=conversation_id)

    def on_runtime_event(self, conversation_id, _request_id, event):
        if isinstance(event.data, dict) and 'subtask' in event.data:
            self.refresh_subtasks(conversation_id)

    def operate(self, action, task):
        if task.kind == 'independent':
            if action == 'open':
                self.open_task(task.conversation_id, task.message_id)
            elif action in {'cancel', 'resume'}:
                self.act(action, task.conversation_id)
        elif action == 'open':
            self.host.chat_view.reveal_subtask(task.conversation_id, task.message_id, task.tool_call_id)

    def prompt_task(self):
        brief, accepted = QInputDialog.getMultiLineText(self.host, '新建独立任务', '任务目标、必要资料与交付要求')
        if accepted and brief.strip():
            self.submit(brief.strip())

    def submit(self, brief, *, from_composer=False):
        host = self.host
        try:
            conv = host.conversation_presenter.ensure_current_conversation_shell()
            if not host.services.conv_service.exists(conv.id) and not host.services.conv_service.save(conv):
                raise ValueError('无法保存来源会话。')
            if not host.chat_view.conversation_id:
                host.chat_view.load_conversation(conv)
            previous = self._submissions.get(conv.id)
            if previous and previous[2]:
                return
            dispatch_id = previous[1] if previous and previous[0] == brief else uuid.uuid4().hex
            request = RunRequest(text='/background ' + brief, conversation_id=conv.id)
            draft = host.input_area.text_input.toPlainText().strip() if from_composer else ''
            self._submissions[conv.id] = (brief, dispatch_id, True)
            self._schedule(conv.id, host.services.command_service.dispatch(request, source='desktop', dispatch_id=dispatch_id),
                           submitted_text=draft, dispatch_id=dispatch_id)
        except Exception as exc:
            host.chat_view.show_notice(str(exc), tone='error')

    def act(self, action, task_id):
        operation = self.service.cancel(task_id) if action == 'cancel' else self.service.resume(task_id)
        self._schedule(self.host.chat_view.conversation_id, operation)

    def _schedule(self, source_id, operation, *, submitted_text='', dispatch_id=''):
        async def execute():
            result, error = None, None
            try:
                result = await operation
            except Exception as exc:
                error = str(exc)
            if not self._disposed:
                self.completed.emit(source_id, result, error, submitted_text, dispatch_id)
        runs = self.host.services.run_service
        execution = execute()
        try:
            runs.schedule(runs.background(execution))
        except Exception as exc:
            execution.close()
            operation.close()
            self._finished(source_id, None, str(exc), submitted_text, dispatch_id)

    def _finished(self, source_id, result, error, submitted_text, dispatch_id):
        previous = self._submissions.get(source_id)
        if previous and previous[1] == dispatch_id:
            if error:
                self._submissions[source_id] = (previous[0], dispatch_id, False)
            else:
                self._submissions.pop(source_id)
        if not error and submitted_text and self.host.chat_view.conversation_id == source_id:
            self.host.input_area.confirm_message_sent(submitted_text, [])
        if not error and dispatch_id and self.host.chat_view.conversation_id == source_id:
            self.host.settings_presenter.toggle_inspector_panel(True)
            self.host.inspector_panel.tabs.setCurrentIndex(0)
        self.host.chat_view.show_notice(error or '独立任务状态已更新',
            tone='error' if error else 'success', conversation_id=source_id)
        self.refresh()

    def open_task(self, task_id, message_id=''):
        self.host.conversation_presenter.select(task_id)
        if message_id:
            QTimer.singleShot(0, lambda: self.host.chat_view.reveal_message(task_id, message_id))

    def abandon(self):
        self._disposed = True
        if self._job:
            self._job.abandon()
            self._job = None
