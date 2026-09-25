"""Scoped, asynchronous projection of existing process services into one window."""
from __future__ import annotations

import uuid
from collections import deque

from PyQt6.QtCore import QCoreApplication, QObject, QThreadPool, QTimer, pyqtSignal

from pycat.gui.dialogs.shell_window import ShellWindow
from pycat.gui.runtime.background_job import BackgroundJob


class ShellPresenter(QObject):
    _opened = pyqtSignal(str, object, object)

    def __init__(self, host):
        super().__init__(host)
        self._host = host
        self.window = None
        self._jobs = set()
        self._read_pending = False
        self._list_pending = False
        self._selection_request = None
        self._cursors = {}
        self._eof = set()
        self._input = deque()
        self._input_bytes = 0
        self._write_pending = False
        self._disposed = False
        self._tasks = set()
        self._opened.connect(self._finish_open)
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._read)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(2000)
        self._refresh_timer.timeout.connect(self.refresh)
        host.destroyed.connect(self.dispose)

    def open(self, process_id=""):
        conversation = self._host.current_conversation
        if conversation is None:
            conversation = self._host.conversation_presenter.ensure_current_conversation_shell()
            self._host.current_conversation = conversation
        if not self._host.services.conv_service.exists(conversation.id):
            if not self._host.services.conv_service.save(conversation):
                return
        if self.window is None:
            self.window = ShellWindow(self._host)
            self.window.new_requested.connect(self.new_terminal)
            self.window.stop_requested.connect(self.stop)
            self.window.control_requested.connect(self.control)
            self.window.input_requested.connect(self.write)
            self.window.response_requested.connect(self.respond)
            self.window.resize_requested.connect(self.resize)
            self.window.selected.connect(self._read)
            self.window.view_discarded.connect(self._discard_view)
        self.window.set_scope(conversation.id, conversation.title, conversation.work_dir)
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        self.refresh(selected_id=str(process_id or ""))
        self._timer.start()
        self._refresh_timer.start()

    def _job(self, operation, done):
        if self._disposed:
            return
        job = BackgroundJob(operation)
        self._jobs.add(job)

        def finished(result, error):
            self._jobs.discard(job)
            if not self._disposed:
                done(result, error)

        job.signals.finished.connect(finished)
        QThreadPool.globalInstance().start(job)

    def _notice(self, error):
        if self.window:
            self.window.notice.setText(str(error))

    def refresh(self, *, selected_id=""):
        if not self.window or not self.window.isVisible():
            return
        scope = self.window.conversation_id
        if selected_id:
            self._selection_request = (scope, selected_id)
        if self._list_pending:
            return
        self._list_pending = True

        def done(result, error):
            self._list_pending = False
            if scope != self.window.conversation_id:
                self.refresh()
                return
            if error:
                self._notice(error)
            else:
                selected = ""
                if self._selection_request and self._selection_request[0] == scope:
                    selected = self._selection_request[1]
                    if any(s.process_id == selected for s in result):
                        self._selection_request = None
                self.window.update_processes(result, selected)

        self._job(lambda: self._host.services.tools.processes(scope, include_exited=True), done)

    def _read(self):
        if not self.window or not self.window.isVisible():
            self._timer.stop()
            self._refresh_timer.stop()
            return
        pid = self.window.process_id
        if not pid or self._read_pending or not self.window.view:
            return
        scope = self.window.conversation_id
        key = (scope, pid)
        if key in self._eof:
            return
        view = self.window.view
        cursor = self._cursors.get(key, 0)
        dimensions = getattr(view, "dimensions", None)
        self._read_pending = True

        def done(result, error):
            self._read_pending = False
            if self.window.cached_view(*key) is not view:
                return
            if error:
                if key == (self.window.conversation_id, self.window.process_id):
                    self._notice(error)
                return
            chunk, frame = result
            self._cursors[key] = chunk.next_cursor
            if frame is not None:
                view.apply_frame(frame, send_responses=False)
            elif chunk.output:
                self.window.append_log(view, chunk.output)
            if chunk.snapshot.error and key == (self.window.conversation_id, self.window.process_id):
                self._notice(chunk.snapshot.error)
            if chunk.snapshot.interactive:
                view.set_input_enabled(chunk.snapshot.running and chunk.snapshot.controller == "user")
            if not chunk.snapshot.running and not chunk.has_more:
                self._eof.add(key)

        def read():
            tools = self._host.services.tools
            chunk = tools.read_process(pid, conversation_id=scope, cursor=cursor)
            frame = view.prepare_output(chunk.output, dimensions) if chunk.snapshot.interactive and (
                chunk.output or dimensions != (view.screen.columns, view.screen.lines)) else None
            if frame:
                for response in frame.responses:
                    tools.terminal_response(pid, response, conversation_id=scope)
            return chunk, frame

        self._job(read, done)

    def new_terminal(self):
        if not self.window or not self.window.new_button.isEnabled():
            return
        scope, operation_id = self.window.conversation_id, uuid.uuid4().hex
        self.window.new_button.setEnabled(False)

        async def execute():
            async def approve(request):
                return await self._host.message_runtime.request_tool_approval(scope, operation_id, request)
            result, error = None, None
            try:
                result = await self._host.services.tools.open_terminal(conversation_id=scope, approval_callback=approve)
            except Exception as exc:
                error = exc
            if not self._disposed:
                self._opened.emit(scope, result, error)

        task = self._host.services.run_service.schedule(execute())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _finish_open(self, scope, result, error):
        if self._disposed:
            return
        self.window.new_button.setEnabled(True)
        if scope != self.window.conversation_id:
            return
        if error or result.is_error:
            self._notice(error or result.content)
        else:
            pid = result.record.get("metadata", {}).get("process_id", "")
            self.refresh(selected_id=pid)
        self._host.conversation_presenter.refresh_processes()

    def write(self, process_id, text):
        if not self.window or not text:
            return
        size = len(text.encode("utf-8"))
        if self._input_bytes + size > 65536:
            self._notice(QCoreApplication.translate('ShellPresenter', '待发送输入已达 64 KiB，请等待后再输入。'))
            return
        self._input.append((self.window.conversation_id, process_id, text))
        self._input_bytes += size
        self._flush_input()

    def _flush_input(self):
        if self._write_pending or not self._input:
            return
        scope, pid, text = self._input.popleft()
        self._input_bytes -= len(text.encode("utf-8"))
        self._write_pending = True

        def done(_result, error):
            self._write_pending = False
            if error:
                self._input = deque(item for item in self._input if item[:2] != (scope, pid))
                self._input_bytes = sum(len(item[2].encode("utf-8")) for item in self._input)
                if (scope, pid) == (self.window.conversation_id, self.window.process_id):
                    self._notice(error)
            self._flush_input()

        self._job(lambda: self._host.services.tools.write_terminal(pid, text, conversation_id=scope), done)

    def respond(self, process_id, text):
        scope = self.window.conversation_id
        self._job(lambda: self._host.services.tools.terminal_response(process_id, text, conversation_id=scope),
                  lambda _r, _e: None)

    def _discard_view(self, scope, process_id):
        key = (scope, process_id)
        self._cursors.pop(key, None)
        self._eof.discard(key)

    def resize(self, process_id, columns, rows):
        scope = self.window.conversation_id
        self._job(lambda: self._host.services.tools.resize_terminal(process_id, columns, rows, conversation_id=scope),
                  lambda _r, error: self._notice(error) if error and scope == self.window.conversation_id else None)

    def control(self, process_id, controller):
        scope = self.window.conversation_id
        self._job(lambda: self._host.services.tools.terminal_control(process_id, conversation_id=scope, controller=controller),
                  lambda _r, error: self._notice(error) if error and scope == self.window.conversation_id else self.refresh())

    def stop(self, process_id, *, conversation_id=None):
        scope = conversation_id or self.window.conversation_id
        self._job(lambda: self._host.services.tools.stop_process(process_id, conversation_id=scope),
                  lambda _r, error: self._notice(error) if error and self.window and scope == self.window.conversation_id else self.refresh())

    def dispose(self):
        if self._disposed:
            return
        self._disposed = True
        self._timer.stop()
        self._refresh_timer.stop()
        for task in tuple(self._tasks):
            task.cancel()
        for job in tuple(self._jobs):
            job.abandon()
        self._jobs.clear()
        if self.window:
            self.window.dispose()
