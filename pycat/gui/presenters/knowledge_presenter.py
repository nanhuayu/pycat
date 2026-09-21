"""Coordinate the existing application projections and one detail lifecycle."""
from copy import copy
from PyQt6.QtCore import QThreadPool
from pycat.gui.dialogs.content_preview import ContentPreviewDialog
from pycat.gui.runtime.background_job import BackgroundJob


class KnowledgePresenter:
    def __init__(self, host):
        self.host = host
        self.detail = None
        self.panel = None
        self._scope = None
        self._jobs = {}

    def bind(self, panel):
        self.panel = panel
        panel.projection_changed.connect(self.context_changed)
        panel.materials.refresh_requested.connect(self.refresh_materials)
        panel.materials.opened.connect(lambda row: self.open_content(ref=row["ref"], change=row.get("change")))
        panel.memory.opened.connect(self.open_memory)
        panel.memory.retry_requested.connect(self.retry_memory)
        panel.memory.assign_requested.connect(self.assign_memory)
        self.context_changed(self.host.current_conversation)

    def show_library(self):
        self.host.settings_presenter.toggle_inspector_panel(True)
        self.panel.tabs.setCurrentWidget(self.panel.materials)
        self.refresh_materials()

    def _run(self, name, operation, finished):
        old = self._jobs.pop(name, None)
        if old:
            old.abandon()
        scope = self._scope
        job = BackgroundJob(operation)
        self._jobs[name] = job
        def complete(result, error):
            if self._jobs.get(name) is not job or scope != self._scope:
                return
            self._jobs.pop(name)
            finished(result, error)
        job.signals.finished.connect(complete)
        QThreadPool.globalInstance().start(job)

    def context_changed(self, conversation, *, refresh_detail=True):
        scope = (getattr(conversation, "id", ""), getattr(conversation, "work_dir", ""))
        changed = scope != self._scope
        self._scope = scope
        self.panel.materials.conversation = conversation
        self.panel.materials.dirty = True
        if changed:
            for job in self._jobs.values():
                job.abandon()
            self._jobs.clear()
            self.panel.materials.offset = 0
            self.panel.materials.items.clear()
            self.panel.memory.set_workspace(scope[1])
            if self.detail and not self.detail.dirty and not self.detail.busy:
                self.detail.close()
        if conversation is None:
            self.panel.materials.set_status("选择会话后查看资料")
            self.panel.memory.apply({"status": "选择会话后查看记忆"})
            return
        self.refresh_memory()
        if self.panel.materials.isVisible():
            self.refresh_materials()
        detail = self.detail
        if refresh_detail and not changed and detail and detail.isVisible() and detail._page.get("kind") in {"wiki", "memory"}:
            if detail._editing:
                detail.set_status("资料已更新；当前编辑已保留，保存时将核对版本。")
            elif not detail.busy and not detail.loading:
                detail.reload_current()

    def refresh_materials(self, reindex=False):
        conv = self.panel.materials.conversation
        if conv is None:
            return
        panel = self.panel.materials
        conversation = copy(conv)
        conversation.messages = list(conv.messages)
        kind, query, offset = panel.kind.currentData(), panel.search.text().strip(), panel.offset
        panel.set_status("正在读取…")
        def apply(page, error):
            if error:
                panel.set_status(str(error))
            else:
                panel.apply(page)
        self._run("materials", lambda: self.host.services.knowledge_service.materials(
            conversation, kind=kind, query=query, offset=offset, reindex=reindex), apply)

    def refresh_memory(self):
        conv = self.host.current_conversation
        if conv is None:
            return
        work_dir, enabled = conv.work_dir, bool(conv.settings.get("memory_enabled", True))
        self._run("memory", lambda: self.host.services.knowledge_service.memory_snapshot(work_dir, enabled=enabled),
                  lambda result, error: self.panel.memory.apply(result if error is None else {"status": "存储不可读", "error": str(error)}))

    def ensure_detail(self):
        if self.detail is None:
            self.detail = ContentPreviewDialog(parent=self.host, services=self.host.services,
                provider_for_conversation=lambda conv: next((p for p in self.host.providers if p.id == conv.provider_id), None))
            self.detail.changed.connect(lambda: self.context_changed(self.host.current_conversation, refresh_detail=False))
        return self.detail

    def open_content(self, **request):
        detail = self.ensure_detail()
        conv = self.host.current_conversation
        if request == detail._request and self._scope == (
                getattr(detail.conversation, "id", ""), getattr(detail.conversation, "work_dir", "")) and detail.isVisible():
            detail.raise_()
        else:
            detail.open_request(request, conv)
        return detail

    def open_memory(self, scope, entry_id=""):
        if self.host.current_conversation is not None:
            self.ensure_detail().open_memory(self.host.current_conversation, scope, entry_id)

    def allow_context_change(self, continuation):
        return self.detail is None or self.detail.allow_leave(continuation)

    def retry_memory(self):
        work_dir = self.host.current_conversation.work_dir
        self._run("retry", lambda: self.host.services.knowledge_service.retry_memory(work_dir),
                  lambda _, error: self.refresh_memory() if error is None else self.panel.memory.set_status(str(error)))

    def assign_memory(self):
        conv = copy(self.host.current_conversation)
        def done(result, error):
            if error or not result[0]:
                self.panel.memory.set_status(str(error or result[1]))
            else:
                self.refresh_memory()
        self._run("assign", lambda: self.host.services.knowledge_service.assign_legacy_memory(conv), done)

    def dispose(self):
        for job in self._jobs.values():
            job.abandon()
        self._jobs.clear()
        if self.detail:
            self.detail.dispose()
