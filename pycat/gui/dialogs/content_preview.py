"""One non-modal detail window, with domain-specific writes and shared reading."""
from copy import copy
from dataclasses import replace
from pathlib import Path
import weakref

from PyQt6 import sip
from PyQt6.QtCore import QThreadPool, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QGuiApplication, QKeySequence, QShortcut
from PyQt6.QtWidgets import QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QToolButton, QMenu, QMessageBox, QFileDialog, QWidget

from pycat.core.content.markdown import strip_frontmatter
from pycat.gui.runtime.background_job import BackgroundJob
from pycat.gui.runtime.content_navigation import ContentOpenUseCase, PreparedPreview
from pycat.gui.widgets.content_viewer import ContentViewer
from pycat.gui.widgets.themed_line_edit import ThemedPlainTextEdit
from pycat.gui.utils.window_geometry import apply_window_size
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.theme import COMPACT_CONTROL_HEIGHT, configure_icon_button, prepare_context_menu
from pycat.models.contracts.content import ContentRef
from pycat.models.session_paths import has_active_workspace


def show_content(parent, **request):
    """Route a widget shortcut to its window's existing detail owner."""
    node = parent
    while node is not None:
        presenter = getattr(node, "knowledge_presenter", None)
        if presenter is not None:
            return presenter.open_content(**request)
        if isinstance(node, ContentPreviewDialog):
            node.open_request(request, node.conversation)
            return node
        node = node.parentWidget()
    owner = parent.window() if parent is not None else None
    dialog = getattr(owner, "_content_detail", None)
    if dialog is None:
        dialog = ContentPreviewDialog(parent=owner)
        if owner is not None:
            owner._content_detail = dialog
    dialog.open_request(request)
    return dialog


class ContentPreviewDialog(QDialog):
    changed = pyqtSignal()

    def __init__(self, target=None, parent=None, *, services=None, provider_for_conversation=None, system_open=None):
        super().__init__(parent)
        self.services = services
        self._provider = provider_for_conversation
        self._system_open = system_open
        self.conversation = None
        self.target = None
        self._request, self._page = {}, {}
        self._history = []
        self._read_job = self._write_job = None
        self._generation = 0
        self.loading = self.busy = self._editing = False
        self._initial_text = ""
        self._return_focus = None
        self.setObjectName("content_preview_dialog")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle("内容")
        apply_window_size(self, preferred=(800, 620), minimum=(360, 320))
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)
        self.toolbar = QWidget()
        self.toolbar.setObjectName("content_toolbar")
        self.toolbar.setFixedHeight(COMPACT_CONTROL_HEIGHT)
        self.commands = QHBoxLayout(self.toolbar)
        self.commands.setContentsMargins(0, 0, 0, 0)
        self.commands.setSpacing(4)
        root.addWidget(self.toolbar)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setProperty("muted", True)
        root.addWidget(self.status)
        self.viewer = ContentViewer()
        root.addWidget(self.viewer, 1)
        self.editor = ThemedPlainTextEdit()
        self.editor.setAccessibleName("编辑内容")
        root.addWidget(self.editor, 1)
        self.back = self._command("返回", Icons.ARROW_LEFT, self.go_back)
        self.source_mode = self._command("显示 Markdown 源码", Icons.CODE)
        self.source_mode.defaultAction().setCheckable(True)
        self.source_mode.defaultAction().toggled.connect(self.viewer.set_source_visible)
        self.previous = self._command("上一页", Icons.CHEVRON_LEFT, lambda: self.set_pdf_page(self.viewer.page - 1))
        self.counter = QLabel()
        self.counter.setAccessibleName("PDF 页码")
        self.commands.addWidget(self.counter)
        self.next = self._command("下一页", Icons.CHEVRON_RIGHT, lambda: self.set_pdf_page(self.viewer.page + 1))
        self.zoom_out = self._command("缩小", Icons.MINUS, lambda: self.viewer.set_zoom(self.viewer.zoom_factor / 1.2))
        self.zoom_reset = self._command("100%", Icons.REFRESH, lambda: self.viewer.set_zoom(1), text=True)
        self.zoom_reset.setFixedWidth(50)
        self.zoom_reset.setToolTip("恢复 100% · 预览比例")
        self.zoom_in = self._command("放大", Icons.PLUS, lambda: self.viewer.set_zoom(self.viewer.zoom_factor * 1.2))
        self.zoom_fit = self._command("适应窗口", Icons.FIT_IMAGE, self.viewer.fit_image)
        self.zoom_fit.setToolTip("适应窗口 · 拖动平移 · Ctrl + 滚轮缩放")
        self.viewer.zoom_changed.connect(self._sync_zoom)
        self.edit = self._command("编辑", Icons.EDIT, self.start_edit)
        self.promote = self._command("整理为项目知识", Icons.BOOK, self.promote_to_knowledge)
        self.save = self._command("保存", Icons.SAVE, self.save_changes, text=True)
        self.save.defaultAction().setShortcut(QKeySequence.StandardKey.Save)
        self.cancel = self._command("取消", Icons.XMARK, self.cancel_edit, text=True)
        self.source_button = self._command("查看来源", Icons.LINK)
        self.source_menu = prepare_context_menu(QMenu("来源", self.source_button), self)
        self.source_button.setMenu(self.source_menu)
        self.source_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.source_menu.aboutToShow.connect(lambda: prepare_context_menu(self.source_menu, self))
        self.commands.addStretch()
        self.copy_button = self._command("复制正文", Icons.COPY, self.copy_preview)
        self.export_button = self._command("另存为", Icons.DOWNLOAD, self.save_as)
        self.more = self._command("更多操作", Icons.MORE)
        self.menu = prepare_context_menu(QMenu(self.more), self)
        self.more.setMenu(self.menu)
        self.more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu.aboutToShow.connect(self._populate_menu)
        self.reload_action = QAction(Icons.get_muted(Icons.REFRESH), "重新读取", self)
        self.reload_action.triggered.connect(self.reload_current)
        self._overflow = []
        self._close_shortcut = QShortcut(QKeySequence("Ctrl+W"), self)
        self._close_shortcut.activated.connect(self.close)
        self.editor.textChanged.connect(self._sync_title)
        self._set_editing(False)
        self.set_status("")
        jobs = self
        self.destroyed.connect(lambda: jobs.dispose())
        if target is not None:
            self.open_request({"target": target})

    @property
    def dirty(self):
        return self._editing and self.editor.toPlainText() != self._initial_text

    def _command(self, label, icon, callback=None, *, text=False):
        action = QAction(Icons.get_muted(icon), label, self)
        if callback is not None:
            action.triggered.connect(lambda _checked=False: callback())
        self.addAction(action)
        button = QToolButton(self.toolbar)
        button.setDefaultAction(action)
        configure_icon_button(button, action.icon(), label)
        if text:
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            button.setFixedWidth(max(40, button.fontMetrics().horizontalAdvance(label) + 16))
        self.commands.addWidget(button)
        return button

    def set_status(self, text):
        self.status.setText(str(text or ""))
        self.status.setVisible(bool(text))

    def _sync_title(self):
        self.setWindowTitle(self._page.get("title", "内容") + (" *" if self.dirty else ""))

    def _sync_commands(self):
        ready = not self.loading and not self.busy
        reading = not self._editing
        pdf = reading and self.viewer.kind == "pdf"
        raster = reading and self.viewer.kind in {"image", "pdf"}
        eligible = {
            self.back: bool(self._history), self.source_mode: reading and self.viewer.kind == "markdown",
            self.previous: pdf, self.counter: pdf, self.next: pdf,
            self.zoom_out: raster, self.zoom_reset: raster, self.zoom_in: raster, self.zoom_fit: raster,
            self.edit: reading and self._page.get("kind") in {"wiki", "memory"} and not self._request.get("evidence"),
            self.promote: reading and self._page.get("kind") == "artifact" and not self._request.get("evidence")
                and self.conversation is not None and has_active_workspace(self.conversation.work_dir),
            self.save: self._editing, self.cancel: self._editing,
            self.source_button: reading and bool(self._page.get("sources")),
            self.copy_button: bool(self._page),
            self.export_button: bool(self.target or self._request.get("image_source")), self.more: True,
        }
        for control, visible in eligible.items():
            control.setVisible(bool(visible))
            if isinstance(control, QToolButton):
                control.defaultAction().setEnabled(ready)
        self.edit.defaultAction().setEnabled(ready and bool(self._page.get("writable")))
        self.edit.setToolTip(self._page.get("write_unavailable") or "编辑")
        self.save.defaultAction().setEnabled(ready and self._editing and bool(self._page.get("writable")))
        self.previous.defaultAction().setEnabled(ready and self.viewer.page > 1)
        self.next.defaultAction().setEnabled(ready and self.viewer.page < self.viewer.pages)
        self.counter.setText(f"{self.viewer.page} / {self.viewer.pages}")
        copy_label = {"image": "复制原图", "pdf": "复制当前页"}.get(self.viewer.kind, "复制正文") if reading else "复制正文"
        self.copy_button.defaultAction().setText(copy_label)
        self.copy_button.setToolTip(copy_label)
        self.copy_button.setAccessibleName(copy_label)
        self.reload_action.setEnabled(ready and bool(self._request))
        self.more.defaultAction().setEnabled(True)
        self._overflow = []
        # A single small row normally fits at 360 px; large system fonts may need overflow.
        available = max(0, self.width() - 12)
        def required_width():
            controls = [control for control in eligible if not control.isHidden()]
            return sum(control.sizeHint().width() if control is self.counter else control.width() for control in controls) + 4 * len(controls)
        for control in (self.export_button, self.source_button, self.promote, self.edit, self.copy_button, self.zoom_fit, self.zoom_reset):
            if required_width() <= available:
                break
            if eligible[control]:
                control.hide()
                self._overflow.append(control)

    def _sync_zoom(self, factor):
        self.zoom_reset.defaultAction().setText(f"{factor:.0%}")

    def _set_editing(self, enabled):
        self._editing = enabled
        self.editor.setVisible(enabled)
        self.viewer.setVisible(not enabled)
        self._sync_commands()
        self._sync_title()

    def choose_leave(self):
        box = QMessageBox(QMessageBox.Icon.Question, "未保存的修改", "保存后离开？", parent=self)
        save = box.addButton("保存", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("放弃", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("留在编辑", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return "save" if box.clickedButton() is save else "discard" if box.clickedButton() is discard else "cancel"

    def allow_leave(self, continuation):
        if self.busy:
            self.set_status("正在保存或整理，请稍候。")
            return False
        if not self.dirty:
            return True
        choice = self.choose_leave()
        if choice == "save":
            self.save_changes(on_success=continuation)
            return False
        if choice == "discard":
            self._set_editing(False)
            return True
        return False

    def open_request(self, request, conversation=None, *, history=False):
        request = dict(request)
        if not self.allow_leave(lambda: self.open_request(request, conversation, history=history)):
            return
        focus = QApplication.focusWidget()
        if not self.isVisible() and focus is not None and not self.isAncestorOf(focus):
            self._return_focus = weakref.ref(focus)
        if history and self._request != request:
            self._history.append((self._request, self.conversation, self.viewer.position()))
            self._history = self._history[-16:]
        elif not history:
            self._history.clear()
        self._request = request
        self.conversation = copy(conversation) if conversation is not None else None
        self._set_editing(False)
        self._load()
        self.show()
        self.raise_()

    def open_memory(self, conversation, scope, entry_id=""):
        self.open_request({"memory": scope, "entry_id": entry_id}, conversation)

    def reload_current(self):
        if self.allow_leave(self.reload_current):
            self._set_editing(False)
            self._load(self.viewer.position(), latest=True)

    def _load(self, position=0, *, latest=False, keep_page=False):
        self.abandon_reads()
        generation = self._generation
        request, conversation, services = dict(self._request), self.conversation, self.services
        request["latest"] = latest
        self.loading = True
        self.target, self._page = None, {}
        self.setWindowTitle("内容")
        if not keep_page:
            self.viewer.apply(PreparedPreview("text"))
        self.source_mode.setChecked(False)
        self.source_menu.clear()
        self.set_status("正在读取…")
        self._sync_commands()
        job = BackgroundJob(lambda: self._read(request, conversation, services))
        self._read_job = job
        def complete(result, error):
            if generation != self._generation:
                return
            self._read_job = None
            self.loading = False
            if error:
                self.set_status(str(error))
                self._sync_commands()
                return
            self.target, preview, self._page = result
            if latest and self.target and self.target.ref.kind == "wiki":
                self._request = {"ref": self.target.ref, "evidence": bool(request.get("evidence"))}
            self.setWindowTitle(self._page["title"])
            self.set_status("\n".join(part for part in (self._page.get("status", ""), preview.notice) if part))
            self.viewer.apply(preview)
            self.viewer.restore_position(position)
            self.source_menu.clear()
            sources = self._page.get("sources", [])
            self.source_menu.setTitle(f"来源（{len(sources)}）")
            self.source_button.setToolTip(f"查看来源（{len(sources)}）")
            for index, source in enumerate(sources):
                label = source.get("name") or source.get("id") or "来源"
                action = self.source_menu.addAction(f"{index + 1}. {label}")
                action.triggered.connect(lambda _checked=False, ref=dict(source): self.open_source(ref))
            self._set_editing(False)
            if request.get("memory") and not request.get("entry_id") and self._page.get("writable"):
                self.start_edit()
        job.signals.finished.connect(complete)
        QThreadPool.globalInstance().start(job)

    @staticmethod
    def _read(request, conversation, services):
        if request.get("error"):
            raise ValueError(request["error"])
        if request.get("memory"):
            snapshot = services.knowledge_service.memory_snapshot(
                conversation.work_dir, enabled=bool(conversation.settings.get("memory_enabled", True)))
            target = next(item for item in snapshot["targets"] if item["target"] == request["memory"])
            entry = next((item for item in target["records"] if item["id"] == request.get("entry_id")), None)
            if request.get("entry_id") and entry is None:
                raise ValueError("这条记忆已被删除，请重新选择。")
            page = {**(entry or {}), "kind": "memory", "title": "项目记忆" if request["memory"] == "memory" else "用户偏好",
                    "body": entry["text"] if entry else "", "digest": target["digest"],
                    "writable": snapshot["enabled"] and snapshot["readable"], "status": snapshot["status"]}
            if page["status"].startswith("已记住"):
                page["status"] = ""
            if entry and not entry.get("sources"):
                if entry.get("origin") == "legacy":
                    page["status"] = "\n".join(filter(None, [page["status"], "旧版记录 · 尚未附有可核实的来源"]))
            if not snapshot["enabled"]:
                page["write_unavailable"] = "此会话已关闭记忆；重新启用后可编辑或忘记。"
            elif not snapshot["readable"]:
                page["write_unavailable"] = "记忆存储不可读，无法修改。"
            return None, PreparedPreview("text", text=page["body"]), page
        if request.get("image_source"):
            return None, PreparedPreview("image", image=ContentOpenUseCase.read_image(request["image_source"])), {"title": "图片"}
        change = request.get("change")
        if change and change.get("deleted"):
            return None, PreparedPreview("text", text=change.get("summary", "")), {
                "title": change["path"], "status": "文件已删除 · 此处保留变更回执"}
        target = request.get("target")
        ref = target.ref if target else ContentRef.from_dict(request["ref"]) if isinstance(request.get("ref"), dict) else request.get("ref")
        if request.get("latest") and isinstance(ref, ContentRef) and ref.kind == "wiki" and not request.get("evidence"):
            # Explicit refresh reads the current version only in the active project.
            if not ContentOpenUseCase._belongs_to_conversation(conversation, ref):
                raise ValueError("该知识不属于当前项目")
            request = {**request, "ref": services.knowledge_service.wiki.read(conversation.work_dir, ref.id)["ref"]}
            target = None
        if target is None:
            if request.get("workspace_path"):
                target = ContentOpenUseCase(services.content_service).resolve_workspace_file(conversation, request["workspace_path"])
            elif request.get("path"):
                target = ContentOpenUseCase(services.content_service).resolve_path(conversation, request["path"]) if (
                    conversation is not None and services is not None) else ContentOpenUseCase.classify_local_file(request["path"])
            else:
                target = ContentOpenUseCase(services.content_service).resolve(
                    conversation, request["ref"], evidence=bool(request.get("evidence")))
        if target.ref.kind == "archive" and target.ref.locator and services is not None:
            preview = PreparedPreview("text", text=services.knowledge_service.source_fragment(target.ref),
                                      notice=f"来源片段 · {target.ref.locator}")
        else:
            preview = ContentOpenUseCase.prepare_preview(target, page=request.get("pdf_page", 1))
        page = {"title": target.name, "kind": target.ref.kind, "status": "原始来源内容" if request.get("evidence") else ""}
        if target.ref.kind == "wiki" and services is not None:
            page = {**services.knowledge_service.wiki.read(target.ref.workspace, target.ref.id), "kind": "wiki",
                    "writable": not request.get("evidence") and ContentOpenUseCase._belongs_to_conversation(conversation, target.ref),
                    "status": ""}
            if page.get("source_errors"):
                page["status"] = "来源已变化，需要重新核实\n" + "\n".join(page["source_errors"])
            preview = replace(preview, text=page["body"], kind="markdown")
        elif target.ref.kind == "artifact":
            preview = replace(preview, text=strip_frontmatter(preview.text), kind="markdown")
        page.setdefault("body", preview.text)
        return target, preview, page

    def start_edit(self):
        if self.loading or self.busy or not self._page.get("writable"):
            return
        self._initial_text = self._page.get("body", "")
        self.editor.setPlainText(self._initial_text)
        self._set_editing(True)
        self.editor.setFocus()

    def cancel_edit(self):
        self._set_editing(False)

    def _mutate(self, operation, finished, *, on_discard=None):
        if self.busy:
            return
        self.busy = True
        self._sync_commands()
        job = BackgroundJob(operation, on_discard=on_discard)
        self._write_job = job
        def complete(result, error):
            if self._write_job is not job:
                return
            self._write_job = None
            self.busy = False
            self._sync_commands()
            if error:
                self.set_status(str(error))
            else:
                finished(result)
        job.signals.finished.connect(complete)
        QThreadPool.globalInstance().start(job)

    def save_changes(self, on_success=None):
        if not self._editing or not self._page.get("writable"):
            return
        page, request, conv = dict(self._page), dict(self._request), self.conversation
        text, service = self.editor.toPlainText(), self.services.knowledge_service
        def operation():
            if request.get("memory"):
                ok, message = service.edit_memory(conv, request["memory"], entry_id=request.get("entry_id", ""),
                                                  text=text, expected_digest=page["digest"])
                if not ok:
                    raise ValueError(message)
                snapshot = service.memory_snapshot(conv.work_dir)
                target = next(item for item in snapshot["targets"] if item["target"] == request["memory"])
                entry = next(item for item in target["records"] if item["text"] == text.strip())
                return {**request, "entry_id": entry["id"]}
            ok, identifier = service.save_knowledge(conv.work_dir, {**page, "body": text, "expected_digest": page["digest"]})
            if not ok:
                raise ValueError(identifier)
            return {"ref": service.wiki.read(conv.work_dir, identifier)["ref"]}
        def saved(new_request):
            self._request = new_request
            self._set_editing(False)
            self.changed.emit()
            if on_success:
                on_success()
            else:
                self._load()
        self._mutate(operation, saved)

    def delete_current(self):
        if not self._page.get("writable") or not self.allow_leave(self.delete_current):
            return
        request, page, conv, service = dict(self._request), dict(self._page), self.conversation, self.services.knowledge_service
        def operation():
            if request.get("memory"):
                ok, message = service.edit_memory(conv, request["memory"], entry_id=request["entry_id"],
                                                  expected_digest=page["digest"], forget=True)
                if not ok:
                    raise ValueError(message)
            else:
                service.delete_knowledge(conv.work_dir, page)
        self._mutate(operation, lambda _: (self.changed.emit(), self.close()))

    def promote_to_knowledge(self):
        if self.busy or self.target is None:
            return
        conv, ref, service = self.conversation, self.target.ref, self.services.knowledge_service
        provider = self._provider(conv) if self._provider else None
        if provider is None:
            self.set_status("请先为会话选择可用模型。")
            return
        def done(page):
            self.changed.emit()
            self.open_request({"ref": page["ref"]}, conv, history=True)
        self._mutate(lambda: service.promote_artifact(conv, ref, provider=provider), done)

    def open_source(self, source):
        if self._editing:
            self.set_status("请先保存或取消编辑，再查看来源。")
            return
        self.open_request({"ref": source, "evidence": True}, self.conversation, history=True)

    def go_back(self):
        if not self._history or not self.allow_leave(self.go_back):
            return
        self._request, self.conversation, position = self._history.pop()
        self._set_editing(False)
        self._load(position)

    def set_pdf_page(self, page):
        if self.viewer.kind == "pdf" and not self.loading:
            self._request = {**self._request, "pdf_page": page}
            self._load(keep_page=True)

    def _populate_menu(self):
        menu = prepare_context_menu(self.menu, self)
        menu.clear()
        for button in reversed(self._overflow):
            if button is self.source_button:
                menu.addMenu(self.source_menu)
            else:
                menu.addAction(button.defaultAction())
        if self._overflow:
            menu.addSeparator()
        menu.addAction(self.reload_action)
        if self.target:
            menu.addAction("系统打开", self.open_system).setEnabled(not self.loading and not self.busy)
            menu.addAction("显示所在文件夹", self.reveal).setEnabled(not self.loading and not self.busy)
        if self._page.get("writable") and (not self._request.get("memory") or self._request.get("entry_id")):
            menu.addSeparator()
            label = "忘记这条记忆" if self._request.get("memory") else "删除知识"
            menu.addAction(label, self._confirm_delete).setEnabled(not self.busy)

    def _confirm_delete(self):
        label = "忘记这条记忆" if self._request.get("memory") else "删除这条知识"
        if QMessageBox.question(self, label, f"确定{label}？") == QMessageBox.StandardButton.Yes:
            self.delete_current()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "reload_action"):
            self._sync_commands()

    def copy_preview(self):
        if self._editing:
            QGuiApplication.clipboard().setText(self.editor.toPlainText())
        elif self.viewer.kind == "image":
            source = self._request.get("image_source")
            if source:
                self._mutate(lambda: ContentOpenUseCase.read_image(source),
                             lambda image: QGuiApplication.clipboard().setImage(image))
            else:
                target = self.target
                self._mutate(lambda: ContentOpenUseCase.prepare_clipboard(target), ContentOpenUseCase.write_clipboard)
        elif self.viewer.kind == "pdf":
            QGuiApplication.clipboard().setImage(self.viewer._image)
        else:
            QGuiApplication.clipboard().setText(self.viewer.text)

    def save_as(self):
        target, source = self.target, self._request.get("image_source")
        from pycat.core.content.export import DOCUMENT_FORMATS
        convertible = bool(target and target.path.suffix.lower() in {".md", ".markdown"})
        filters = {f"{label} (*{suffix})": key for key, (suffix, label) in DOCUMENT_FORMATS.items()} if convertible else {}
        path, selected = QFileDialog.getSaveFileName(self, "另存内容", target.name if target else "image.png",
                                                    ";;".join(filters))
        if not path:
            return
        if target:
            format = filters.get(selected)
            if format == "markdown":
                format = None
            self._mutate(lambda: ContentOpenUseCase.prepare_save_as(target, path, format=format), ContentOpenUseCase.commit_save_as,
                         on_discard=lambda result, _: ContentOpenUseCase.discard_save_as(result))
        elif source:
            self._mutate(lambda: Path(path).write_bytes(ContentOpenUseCase.image_bytes(source)), lambda _: None)

    def open_system(self):
        if self._system_open:
            self._system_open()
        elif self.target:
            target = self.target
            self._mutate(lambda: ContentOpenUseCase.verify_target(target),
                         lambda _: ContentOpenUseCase.open_system(target))

    def reveal(self):
        if self.target:
            target = self.target
            self._mutate(lambda: ContentOpenUseCase.verify_target(target), lambda _: ContentOpenUseCase.reveal(target))

    def abandon_reads(self):
        self._generation += 1
        if self._read_job:
            self._read_job.abandon()
            self._read_job = None
        self.loading = False

    def dispose(self):
        self.abandon_reads()
        if self._write_job:
            self._write_job.abandon()
            self._write_job = None

    def closeEvent(self, event):
        if not self.allow_leave(self.close):
            event.ignore()
            return
        self.abandon_reads()
        self._history.clear()
        self._set_editing(False)
        self.target, self._page, self._request = None, {}, {}
        self.conversation = None
        self.editor.clear()
        self.viewer.apply(PreparedPreview("text"))
        self.source_mode.setChecked(False)
        self.source_menu.clear()
        self.set_status("")
        self._sync_commands()
        super().closeEvent(event)
        focus = self._return_focus() if self._return_focus else None
        self._return_focus = None
        if focus is not None and not sip.isdeleted(focus):
            focus.setFocus()

    def reject(self):
        self.close()
