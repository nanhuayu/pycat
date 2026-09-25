"""Coordinate bounded workspace I/O through AppServices and existing Qt jobs."""
import threading
from copy import copy
from pathlib import Path

from PyQt6.QtCore import QCoreApplication, QThreadPool
from PyQt6.QtWidgets import QFileDialog

from pycat.gui.runtime.background_job import BackgroundJob


class WorkspaceFilesPresenter:
    def __init__(self, host):
        self.host = host
        self.panel = None
        self._scope = None
        self._jobs = {}
        self._cancel = None

    def bind(self, inspector):
        self.panel = inspector.files
        inspector.projection_changed.connect(self.context_changed)
        self.panel.refresh_requested.connect(self.refresh)
        self.panel.open_requested.connect(self.open)
        self.panel.choose_upload_requested.connect(self.choose_upload)
        self.panel.upload_requested.connect(self.upload)
        self.panel.download_requested.connect(self.download)
        self.panel.drag_requested.connect(self.prepare_drag)
        self.panel.cancel_requested.connect(self.cancel_transfer)
        self.context_changed(self.host.current_conversation)

    def context_changed(self, conversation):
        scope = (getattr(conversation, "id", ""), getattr(conversation, "work_dir", ""))
        if scope == self._scope:
            return
        self.dispose()
        self._scope = scope
        self.panel.set_context(scope[1])
        if scope[1] and self.panel.isVisible():
            self.refresh()

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

    def refresh(self, directory=None, *, status=""):
        if not self._scope[1] or self.panel.busy:
            return
        directory = self.panel.directory if directory is None else directory
        work_dir = self._scope[1]
        service = self.host.services.workspace_service
        self.panel.set_loading(True)
        self.panel.items.clear_prepared()
        self.panel.set_status(QCoreApplication.translate('WorkspaceFilesPresenter', '正在读取目录…'))
        def apply(page, error):
            self.panel.set_loading(False)
            if error:
                self.panel.set_status(str(error))
            else:
                self.panel.apply(page)
                if status:
                    self.panel.set_status(status)
        self._run("list", lambda: service.browse_files(work_dir, directory), apply)

    def open(self, row):
        if self.panel.busy or self.panel.loading:
            return
        if row["is_dir"]:
            self.refresh(row["path"])
        else:
            self.host.knowledge_presenter.open_content(workspace_path=row["path"])

    def choose_upload(self):
        scope, directory = self._scope, self.panel.directory
        paths, _ = QFileDialog.getOpenFileNames(self.panel, QCoreApplication.translate('WorkspaceFilesPresenter', '复制文件到工作区'))
        if paths and scope == self._scope:
            self.upload(paths, directory)

    def upload(self, paths, directory):
        work_dir = self._scope[1]
        service = self.host.services.workspace_service
        self._transfer(QCoreApplication.translate('WorkspaceFilesPresenter', '复制到工作区'), paths, lambda path, check: service.upload_file(
            work_dir, directory, path, check_cancelled=check))

    def download(self, rows):
        scope = self._scope
        destination = QFileDialog.getExistingDirectory(self.panel, QCoreApplication.translate('WorkspaceFilesPresenter', '选择下载目录（保留已有同名文件）'))
        if not destination or scope != self._scope:
            return
        conversation = copy(self.host.current_conversation)
        service = self.host.services.workspace_service
        self._transfer(QCoreApplication.translate('WorkspaceFilesPresenter', '下载'), rows, lambda row, check: service.download_file(
            conversation, row["path"], destination, into_directory=True, check_cancelled=check))

    def prepare_drag(self, rows):
        if any(row["is_dir"] for row in rows):
            self.panel.set_status(QCoreApplication.translate('WorkspaceFilesPresenter', '文件夹请先打包；可直接拖出普通文件'))
            return
        conversation = copy(self.host.current_conversation)
        service = self.host.services.workspace_service
        def ready(paths):
            started = self.panel.items.offer_drag(rows, paths)
            self.panel.set_status("" if started else QCoreApplication.translate('WorkspaceFilesPresenter', '文件已准备，可再次拖出到本机文件夹'))
        self._transfer(QCoreApplication.translate('WorkspaceFilesPresenter', '准备拖出'), rows, lambda row, check: service.prepare_file_download(
            conversation, row["path"], check_cancelled=check), ready=ready)

    def _transfer(self, title, items, operation, *, ready=None):
        if not self._scope[1] or self.panel.busy or self.panel.loading:
            return
        if not items or len(items) > 64:
            self.panel.set_status(QCoreApplication.translate('WorkspaceFilesPresenter', '每次请选择 1–64 个文件；单文件上限 128 MiB'))
            return
        cancel = self._cancel = threading.Event()
        def check():
            if cancel.is_set():
                raise InterruptedError(QCoreApplication.translate('WorkspaceFilesPresenter', '传输已取消'))
        def transfer():
            completed, errors = [], []
            for item in items:
                try:
                    check()
                    completed.append(operation(item, check))
                except InterruptedError:
                    break
                except Exception as exc:
                    name = item.get("name", "") if isinstance(item, dict) else Path(item).name
                    errors.append(f"{name}：{exc}")
            return completed, errors
        self.panel.set_busy(True)
        self.panel.set_status(QCoreApplication.translate('WorkspaceFilesPresenter', '{title}中 · {value} 个文件…').format(title=title, value=len(items)))
        def complete(result, error):
            self.panel.set_busy(False)
            self._cancel = None
            completed, errors = result if error is None else ([], [str(error)])
            if ready and not cancel.is_set() and not errors:
                ready(completed)
                return
            outcome = QCoreApplication.translate('WorkspaceFilesPresenter', '已取消') if cancel.is_set() else QCoreApplication.translate('WorkspaceFilesPresenter', '{title}完成').format(title=title)
            status = QCoreApplication.translate('WorkspaceFilesPresenter', '{outcome} · {completed}/{total} 个文件').format(outcome=outcome, completed=len(completed), total=len(items))
            if errors:
                status += QCoreApplication.translate('WorkspaceFilesPresenter', ' · {value} 个失败\n').format(value=len(errors)) + "\n".join(errors)
            self.panel.set_status(status)
            if ready is None:
                self.refresh(status=status)
        self._run("transfer", transfer, complete)

    def cancel_transfer(self):
        if self._cancel:
            self._cancel.set()
            self.panel.set_status(QCoreApplication.translate('WorkspaceFilesPresenter', '正在取消；已完成的文件会保留…'))

    def dispose(self):
        if self._cancel:
            self._cancel.set()
            self._cancel = None
        for job in self._jobs.values():
            job.abandon()
        self._jobs.clear()
