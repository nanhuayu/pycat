"""Shared projection and actions for one verified conversation content ref."""
from __future__ import annotations

import logging
from functools import partial
from typing import Any, Callable

from PyQt6.QtCore import QCoreApplication, QSize, Qt, QThreadPool, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QFileDialog, QMenu, QMessageBox, QVBoxLayout, QWidget

from pycat.core.content.references import content_identity
from pycat.gui.dialogs.content_preview import show_content
from pycat.gui.runtime.background_job import BackgroundJob
from pycat.gui.runtime.content_navigation import (
    ContentNavigationError,
    ContentOpenTarget,
    ContentOpenUseCase,
    ContentTargetResolver,
)
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.image_loader import read_image
from pycat.gui.utils.theme import prepare_context_menu
from pycat.gui.widgets.capsule import SingleLineLabel
from pycat.gui.widgets.image_thumbnail import ImageThumbnail
from pycat.gui.widgets.workflow_capsule import WorkflowCapsuleRow
from pycat.models.contracts.content import ContentRef
from pycat.models.workspace import workspace_identity

logger = logging.getLogger(__name__)


class ContentRefWidget(QWidget):
    """Render a ref as an image thumbnail or file row with one action model."""

    image_edit_requested = pyqtSignal(str)

    def __init__(
        self,
        ref: ContentRef,
        *,
        resolve_target: ContentTargetResolver,
        context_label: str = "",
        compact_image: bool = False,
        allow_image_edit: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.ref = ref
        self._compact_image = compact_image
        self._allow_image_edit = allow_image_edit
        self._resolve_target = resolve_target
        # Resolve the stable row role before translating its display label.
        self._context_kind = {
            "输入": "input", "交付": "output", "成果": "artifact",
        }.get(context_label, str(ref.kind or "content"))
        self._context_label = {
            "输入": QCoreApplication.translate('ContentRefWidget', '输入'),
            "交付": QCoreApplication.translate('ContentRefWidget', '交付'),
            "成果": QCoreApplication.translate('ContentRefWidget', '成果'),
        }.get(context_label, str(context_label or QCoreApplication.translate('ContentRefWidget', '内容')))
        self.target: ContentOpenTarget | None = None
        self.error = ""
        self._content_widget: QWidget | None = None
        self._jobs: set[BackgroundJob] = set()
        self._active_job: BackgroundJob | None = None
        self._active_action: Callable[[ContentOpenTarget], object] | None = None
        self._active_finalize: Callable[[ContentOpenTarget, object], object] | None = None
        self._active_cleanup: Callable[[object], None] | None = None

        def abandon_jobs(_object=None, jobs=self._jobs) -> None:
            for job in tuple(jobs):
                job.abandon()
            jobs.clear()
            self._active_job = None

        self.destroyed.connect(abandon_jobs)

        self.setObjectName("content_ref_widget")
        self.setSizePolicy(self.sizePolicy().horizontalPolicy(), self.sizePolicy().verticalPolicy())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._resolve_and_render()

    def _resolve_and_render(self) -> None:
        try:
            self.target = self._resolve_target(self.ref, verify_integrity=False)
            self.error = ""
        except Exception as exc:
            self.target = None
            self.error = str(exc or QCoreApplication.translate('ContentRefWidget', '内容无法打开')).strip() or QCoreApplication.translate('ContentRefWidget', '内容无法打开')
        self.setProperty("state", "ready" if self.target is not None else "failed")
        if self.target is not None and self.target.is_image:
            self._render_image(self.target)
        else:
            self._render_file(self.target)

    def _render_image(self, target: ContentOpenTarget) -> None:
        thumbnail = ImageThumbnail(str(target.path) if target.path else "", self,
            load_image=partial(_read_thumbnail, target) if target.materialize else None)
        thumbnail.setToolTip(self._tooltip(target))
        thumbnail.clicked.connect(self.open_default)
        self._bind_context_menu(thumbnail)
        self.layout().addWidget(thumbnail, 0, Qt.AlignmentFlag.AlignLeft)
        if self._compact_image:
            self.setFixedSize(112, 108)
            name = SingleLineLabel(target.name)
            name.setToolTip(self._tooltip(target))
            self.layout().addWidget(name)
        self._content_widget = thumbnail
        self.setToolTip(thumbnail.toolTip())

    def _render_file(self, target: ContentOpenTarget | None) -> None:
        row = WorkflowCapsuleRow(
            kind=self._context_kind,
            status="completed" if target is not None else "failed",
            payload=self.ref,
            file_path=str(target.path) if target is not None and target.path else "",
            parent=self,
        )
        row.set_content(
            icon=Icons.get_muted(Icons.FILE_LINES),
            title=str(getattr(self.ref, "name", "") or QCoreApplication.translate('ContentRefWidget', '文件')),
            meta=self._context_label if target is None else self._file_meta(target),
        )
        tooltip = self._tooltip(target)
        row.setToolTip(tooltip)
        row.set_interactive(True)
        row.clicked.connect(lambda _payload: self.open_default())
        self._bind_context_menu(row)
        self.layout().addWidget(row)
        self._content_widget = row
        self.setToolTip(tooltip)

    def _bind_context_menu(self, widget: QWidget) -> None:
        widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        widget.customContextMenuRequested.connect(
            lambda position, source=widget: self._show_context_menu(source.mapToGlobal(position))
        )

    def _show_context_menu(self, global_position) -> None:
        self.create_context_menu().exec(global_position)

    def create_context_menu(self) -> QMenu:
        menu = prepare_context_menu(QMenu(self), self)
        if self.target is not None and self.target.preview_kind:
            menu.addAction(QCoreApplication.translate('ContentRefWidget', '预览'), self.preview)
        if self.target is not None and self.target.is_image and self._allow_image_edit:
            menu.addAction(QCoreApplication.translate('ContentRefWidget', '继续编辑图片'), self.continue_edit)
        menu.addAction(QCoreApplication.translate('ContentRefWidget', '系统打开'), self.open_system)
        menu.addAction(QCoreApplication.translate('ContentRefWidget', '复制'), self.copy_to_clipboard)
        menu.addAction(QCoreApplication.translate('ContentRefWidget', '另存为'), self.save_as)
        menu.addAction(QCoreApplication.translate('ContentRefWidget', '显示所在文件夹'), self.reveal)
        return menu

    def _current_target(self) -> ContentOpenTarget:
        try:
            target = self._resolve_action_target()
        except ContentNavigationError:
            raise
        except Exception as exc:
            raise ContentNavigationError(str(exc or QCoreApplication.translate('ContentRefWidget', '内容无法打开'))) from exc
        self.target = target
        self.error = ""
        return target

    def _resolve_action_target(self) -> ContentOpenTarget:
        try:
            return self._resolve_target(self.ref, verify_integrity=True)
        except ContentNavigationError:
            raise
        except Exception as exc:
            raise ContentNavigationError(str(exc or QCoreApplication.translate('ContentRefWidget', '内容无法打开'))) from exc

    def open_default(self) -> None:
        self._start_resolved_action(self._open_default_target)

    def _open_default_target(self, target: ContentOpenTarget) -> None:
        show_content(self, target=target)

    def preview(self) -> None:
        self.open_default()

    def continue_edit(self) -> None:
        def edit(target: ContentOpenTarget) -> None:
            if target.is_image:
                self.image_edit_requested.emit(target.ref.ref)
        self._start_resolved_action(edit)

    def open_system(self) -> None:
        def open_target(target: ContentOpenTarget) -> None:
            if not ContentOpenUseCase.open_system(target):
                raise ContentNavigationError(QCoreApplication.translate('ContentRefWidget', '系统没有可用的打开方式'))

        self._start_resolved_action(open_target)

    def copy_to_clipboard(self) -> None:
        self._start_resolved_action(
            worker_action=ContentOpenUseCase.prepare_clipboard,
            finalize_action=lambda _target, payload: ContentOpenUseCase.write_clipboard(payload),
        )

    def save_as(self) -> None:
        try:
            destination, _selected_filter = QFileDialog.getSaveFileName(
                self,
                QCoreApplication.translate('ContentRefWidget', '另存内容'),
                str(getattr(self.ref, "name", "") or "content"),
                QCoreApplication.translate('ContentRefWidget', '所有文件 (*)'),
            )
        except Exception as exc:
            self._show_error(exc)
            return
        if destination:
            self._start_resolved_action(
                worker_action=lambda target: ContentOpenUseCase.prepare_save_as(target, destination),
                finalize_action=lambda _target, prepared: ContentOpenUseCase.commit_save_as(prepared),
                cleanup_worker_result=ContentOpenUseCase.discard_save_as,
            )

    def reveal(self) -> None:
        def reveal_target(target: ContentOpenTarget) -> None:
            if not ContentOpenUseCase.reveal(target):
                raise ContentNavigationError(QCoreApplication.translate('ContentRefWidget', '无法显示所在文件夹'))

        self._start_resolved_action(reveal_target)

    def _start_resolved_action(
        self,
        action: Callable[[ContentOpenTarget], object] | None = None,
        *,
        worker_action: Callable[[ContentOpenTarget], object] | None = None,
        finalize_action: Callable[[ContentOpenTarget, object], object] | None = None,
        cleanup_worker_result: Callable[[object], None] | None = None,
    ) -> None:
        if self._jobs:
            return

        try:
            # Capture the current target on the GUI thread. The worker never
            # calls a resolver closure that can read mutable GUI state.
            target = self._resolve_target(self.ref, verify_integrity=False)
        except ContentNavigationError as exc:
            self._show_error(exc)
            return
        except Exception as exc:
            self._show_error(ContentNavigationError(str(exc or QCoreApplication.translate('ContentRefWidget', '内容无法打开'))))
            return

        def operation():
            if worker_action is None:
                ready = ContentOpenUseCase.prepare_target(target)
                result = None
            else:
                # Copy/save verify the bytes they prepare themselves.
                ready = target.materialize() if target.materialize is not None else target
                result = worker_action(ready)
            return ready, result

        cleanup_result = cleanup_worker_result

        def on_discard(result: Any, _error: Exception | None) -> None:
            ContentRefWidget._cleanup_worker_result(result, cleanup_result)

        job = BackgroundJob(operation, on_discard=on_discard)
        self._jobs.add(job)
        self._active_job = job
        self._active_action = action
        self._active_finalize = finalize_action
        self._active_cleanup = cleanup_worker_result
        self._set_action_busy(True)
        job.signals.finished.connect(self._handle_action_finished)
        QThreadPool.globalInstance().start(job)

    @pyqtSlot(object, object)
    def _handle_action_finished(self, result: object, error: Exception | None) -> None:
        """Finish a background action on the widget's GUI thread."""

        job = self._active_job
        action = self._active_action
        finalize_action = self._active_finalize
        cleanup_worker_result = self._active_cleanup
        self._active_job = None
        self._active_action = None
        self._active_finalize = None
        self._active_cleanup = None
        if job is not None:
            self._jobs.discard(job)
        self._set_action_busy(False)
        if error is not None:
            self._show_error(error)
            return
        if not isinstance(result, tuple) or len(result) != 2:
            self._show_error(ContentNavigationError(QCoreApplication.translate('ContentRefWidget', '内容操作返回了无效结果')))
            return
        target, worker_result = result
        if not isinstance(target, ContentOpenTarget):
            self._show_error(ContentNavigationError(QCoreApplication.translate('ContentRefWidget', '内容操作目标无效')))
            self._cleanup_worker_result(worker_result, cleanup_worker_result)
            return
        self.target = target
        self.error = ""
        if action is None and finalize_action is None:
            return
        try:
            self._ensure_target_is_current(target)
            if finalize_action is not None:
                finalize_action(target, worker_result)
            elif action is not None:
                action(target)
        except Exception as exc:
            self._cleanup_worker_result(worker_result, cleanup_worker_result)
            self._show_error(exc)

    @staticmethod
    def _cleanup_worker_result(
        result: object,
        cleanup: Callable[[object], None] | None,
    ) -> None:
        if cleanup is None:
            return
        payload = result[1] if isinstance(result, tuple) and len(result) == 2 else result
        try:
            cleanup(payload)
        except Exception:
            return

    def _ensure_target_is_current(self, target: ContentOpenTarget) -> None:
        try:
            current = self._resolve_target(self.ref, verify_integrity=False)
        except ContentNavigationError:
            raise
        except Exception as exc:
            raise ContentNavigationError(str(exc or QCoreApplication.translate('ContentRefWidget', '内容无法打开'))) from exc
        same_scope = (
            not target.conversation_id
            or not current.conversation_id
            or target.conversation_id == current.conversation_id
        ) and (
            workspace_identity(target.conversation_workspace) == workspace_identity(current.conversation_workspace)
        )
        same_ref = (
            str(target.ref.kind or "") == str(current.ref.kind or "")
            and str(target.ref.ref or "") == str(current.ref.ref or "")
        )
        same_location = (
            content_identity(target.ref) == content_identity(current.ref)
            if current.materialize is not None else target.path == current.path
        )
        if not same_scope or not same_ref or not same_location:
            raise ContentNavigationError(QCoreApplication.translate('ContentRefWidget', '会话已切换，已取消内容操作'))

    def _set_action_busy(self, busy: bool) -> None:
        self.setProperty("busy", bool(busy))
        if self._content_widget is None:
            return
        if isinstance(self._content_widget, WorkflowCapsuleRow):
            self._content_widget.set_content(icon=Icons.get_muted(Icons.FILE_LINES),
                title=self.ref.name or QCoreApplication.translate('ContentRefWidget', '文件'),
                meta=QCoreApplication.translate('ContentRefWidget', '正在加载…') if busy else self._file_meta(self.target) if self.target else self._context_label)
        self._content_widget.setEnabled(not busy)
        if busy:
            self._content_widget.setCursor(Qt.CursorShape.WaitCursor)
        else:
            self._content_widget.unsetCursor()

    def _show_error(self, error: Exception) -> None:
        detail = str(error or self.error or QCoreApplication.translate('ContentRefWidget', '内容无法打开')).strip() or QCoreApplication.translate('ContentRefWidget', '内容无法打开')
        self.error = detail
        logger.debug("Content action failed for %s: %s", self.ref.ref, detail)
        QMessageBox.warning(self, QCoreApplication.translate('ContentRefWidget', '无法打开内容'), detail)

    def _tooltip(self, target: ContentOpenTarget | None) -> str:
        lines = [
            str(getattr(self.ref, "name", "") or QCoreApplication.translate('ContentRefWidget', '内容')),
            str(getattr(self.ref, "ref", "") or ""),
        ]
        if target is not None:
            size = self._target_size(target)
            if size is None:
                lines.append(QCoreApplication.translate('ContentRefWidget', '{mime} · 内容当前不可用').format(mime=target.mime))
            else:
                lines.append(f"{target.mime} · {self._format_file_size(size)}")
            lines.append(
                QCoreApplication.translate('ContentRefWidget', '点击预览')
                if target.preview_kind in {"image", "text", "pdf"}
                else QCoreApplication.translate('ContentRefWidget', '点击打开')
            )
            if target.materialize is not None:
                lines.append(QCoreApplication.translate('ContentRefWidget', '远程文件 · 打开时下载'))
        else:
            lines.append(self.error or QCoreApplication.translate('ContentRefWidget', '内容不可用'))
        return "\n".join(line for line in lines if line)

    def _file_meta(self, target: ContentOpenTarget) -> str:
        size = self._target_size(target)
        if size is None:
            return QCoreApplication.translate('ContentRefWidget', '{_context_label} · 内容当前不可用').format(_context_label=self._context_label)
        return f"{self._context_label} · {self._format_file_size(size)}"

    @staticmethod
    def _target_size(target: ContentOpenTarget) -> int | None:
        if target.path is None:
            return int(target.ref.size or 0)
        try:
            return int(target.path.stat().st_size)
        except OSError:
            return None

    @staticmethod
    def _format_file_size(size: int) -> str:
        value = max(0, int(size or 0))
        if value < 1024:
            return f"{value} B"
        if value < 1024 * 1024:
            return f"{value / 1024:.1f} KB"
        return f"{value / (1024 * 1024):.1f} MB"


def _read_thumbnail(target):
    ready = ContentOpenUseCase.prepare_target(target)
    return read_image(str(ready.path), max_size=QSize(80, 80))
