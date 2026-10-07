"""One non-modal detail window, with domain-specific writes and shared reading."""
import hashlib
import os
import weakref
from copy import copy
from dataclasses import replace
from pathlib import Path

from PyQt6 import sip
from PyQt6.QtCore import QSignalBlocker, Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QGuiApplication, QKeySequence, QShortcut, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pycat.core.commands.mentions import utf16_to_python
from pycat.core.content.markdown import strip_frontmatter
from pycat.core.content.references import content_identity
from pycat.core.content.resolver import SessionContentResolver
from pycat.gui.runtime.background_job import BackgroundJob
from pycat.gui.runtime.content_navigation import ContentOpenUseCase, PreparedPreview
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.theme import COMPACT_CONTROL_HEIGHT, configure_icon_button, prepare_context_menu
from pycat.gui.utils.window_geometry import apply_window_size
from pycat.gui.widgets.content_chat import ContentChat
from pycat.gui.widgets.content_viewer import ContentViewer
from pycat.gui.widgets.themed_line_edit import ThemedLineEdit
from pycat.models.contracts.content import ContentRef
from pycat.models.conversation import Conversation
from pycat.models.session_paths import has_active_workspace


def show_content(parent, **request):
    """Route a widget shortcut to its window's existing detail owner."""
    node = parent
    while node is not None:
        presenter = getattr(node, "knowledge_presenter", None)
        if presenter is not None:
            return presenter.open_content(**request)
        if isinstance(node, ContentPreviewDialog):
            owner = node._owner_reference() if node._owner_reference else None
            presenter = getattr(owner, "knowledge_presenter", None)
            if presenter is not None:
                return presenter.open_content(**request)
            node.open_request(request, node.conversation)
            return node
        node = node.parentWidget()
    owner = parent.window() if parent is not None else None
    dialogs = getattr(owner, "_content_details", [])
    dialog = next((item for item in dialogs if item.matches_request(request, None)), None)
    if dialog is None:
        dialog = ContentPreviewDialog(parent=owner)
        if owner is not None:
            owner._content_details = dialogs
            dialogs.append(dialog)
            owner.destroyed.connect(dialog.dispose)
        def release(_result):
            if dialog in dialogs:
                dialogs.remove(dialog)
            dialog.dispose()
        dialog.finished.connect(release)
    dialog.open_request(request)
    return dialog


class ContentPreviewDialog(QDialog):
    changed = pyqtSignal()
    attach_to_chat = pyqtSignal(str)

    def __init__(self, target=None, parent=None, *, services=None, provider_for_conversation=None, system_open=None):
        # The presenter owns the Python lifetime; a native Qt parent would
        # turn this into an owned secondary window without its own taskbar entry.
        super().__init__(None, Qt.WindowType.Window)
        self._owner_reference = weakref.ref(parent) if parent is not None else None
        self._disposed = False
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
        self._library_item = None
        self._content_key = ''
        self._saved_annotations = []
        self.setObjectName("content_preview_dialog")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle("内容")
        self.setWindowIcon(Icons.brand())
        self.setWindowFlag(Qt.WindowType.WindowMinMaxButtonsHint, True)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setSizeGripEnabled(True)
        apply_window_size(self, preferred=(1100, 700), minimum=(360, 320))
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
        self.body = QSplitter(Qt.Orientation.Horizontal)
        self.body.setChildrenCollapsible(False)
        self.body.setHandleWidth(1)
        self.body.addWidget(self.viewer)
        root.addWidget(self.body, 1)
        self.chat = ContentChat(services, self.capture_content, self) if services and getattr(services, 'content_chat_service', None) else None
        if self.chat:
            self.body.addWidget(self.chat)
            self.body.setStretchFactor(0, 1)
            self.body.setStretchFactor(1, 0)
            self.body.setSizes([720, 320])
            self.chat.apply_requested.connect(self.apply_suggestion)
            self.viewer.selection_requested.connect(self._quote_selection)
            self.chat.annotation_panel.selection_requested.connect(self.viewer.picture.select_annotation)
            self.chat.annotation_panel.note_changed.connect(self.viewer.picture.update_annotation)
            self.chat.annotation_panel.delete_requested.connect(self.viewer.picture.delete_annotation)
        self._chat_visible = True
        self._chat_force = False
        self.editor = self.viewer.raw
        self.editor.setAccessibleName("编辑内容")
        self.back = self._command(self.tr("返回"), Icons.ARROW_LEFT, self.go_back)
        self.read_mode = self._command(self.tr("阅读"), Icons.EYE)
        self.source_mode = self._command(self.tr("源码"), Icons.CODE)
        self.preview_modes = QActionGroup(self)
        self.preview_modes.setExclusive(True)
        for button in (self.read_mode, self.source_mode):
            button.defaultAction().setCheckable(True)
            self.preview_modes.addAction(button.defaultAction())
        self.read_mode.setChecked(True)
        self.source_mode.defaultAction().toggled.connect(self.viewer.set_source_visible)
        self.previous = self._command(self.tr("上一页"), Icons.CHEVRON_LEFT, lambda: self.set_pdf_page(self.viewer.page - 1))
        self.counter = QLabel()
        self.counter.setAccessibleName("PDF 页码")
        self.commands.addWidget(self.counter)
        self.next = self._command(self.tr("下一页"), Icons.CHEVRON_RIGHT, lambda: self.set_pdf_page(self.viewer.page + 1))
        self.zoom_out = self._command(self.tr("缩小"), Icons.MINUS, lambda: self.viewer.set_zoom(self.viewer.zoom_factor / 1.2))
        self.zoom_reset = self._command("100%", Icons.REFRESH, lambda: self.viewer.set_zoom(1), text=True)
        self.zoom_reset.setFixedWidth(50)
        self.zoom_reset.setToolTip("恢复 100% · 预览比例")
        self.zoom_in = self._command(self.tr("放大"), Icons.PLUS, lambda: self.viewer.set_zoom(self.viewer.zoom_factor * 1.2))
        self.zoom_fit = self._command(self.tr("适应窗口"), Icons.FIT_IMAGE, self.viewer.fit_image)
        self.zoom_fit.setToolTip("适应窗口 · 拖动平移 · Ctrl + 滚轮缩放")
        self.viewer.zoom_changed.connect(self._sync_zoom)
        self.edit = self._command(self.tr("编辑"), Icons.EDIT, self.start_edit)
        self.promote = self._command(self.tr("整理为项目知识"), Icons.BOOKS, self.promote_to_knowledge)
        self.save = self._command(self.tr("保存"), Icons.SAVE, self.save_changes, text=True)
        self.save.setProperty("primary", True)
        self.save.defaultAction().setShortcut(QKeySequence.StandardKey.Save)
        self.cancel = self._command(self.tr("取消"), Icons.XMARK, self.cancel_edit, text=True)
        self.source_button = self._command(self.tr("查看来源"), Icons.LINK)
        self.source_menu = prepare_context_menu(QMenu("来源", self.source_button), self)
        self.source_button.setMenu(self.source_menu)
        self.source_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.source_menu.aboutToShow.connect(lambda: prepare_context_menu(self.source_menu, self))
        self.favorite = self._command(self.tr('收藏'), Icons.STAR, self.toggle_favorite)
        self.favorite.defaultAction().setCheckable(True)
        self.chat_toggle = self._command(self.tr('内容对话'), Icons.CHAT, self.toggle_chat)
        self.chat_toggle.defaultAction().setCheckable(True)
        self.annotation = self._command(self.tr('圈选图片区域'), Icons.REGION)
        self.annotation.defaultAction().setCheckable(True)
        self.annotation.defaultAction().toggled.connect(self.viewer.picture.set_annotation_mode)
        self.viewer.picture.annotation_selected.connect(self.select_annotation)
        self.viewer.picture.annotation_edit_requested.connect(self.focus_annotation_note)
        self.viewer.picture.annotations_changed.connect(self.annotations_changed)
        self.commands.addStretch()
        self.search = ThemedLineEdit()
        self.search.setPlaceholderText(self.tr('搜索文档 · Ctrl+F'))
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(172)
        self.search.returnPressed.connect(lambda: self.viewer.find_text(self.search.text()))
        self.search.textEdited.connect(lambda text: self.viewer.find_text(text))
        self.commands.addWidget(self.search)
        self.wrap = self._command(self.tr('自动换行'), Icons.WRAP_TEXT)
        self.wrap.defaultAction().setCheckable(True)
        self.wrap.defaultAction().setChecked(True)
        self.wrap.defaultAction().toggled.connect(self.viewer.set_wrapped)
        self.line_numbers = self._command(self.tr('显示行号'), Icons.LINE_NUMBERS)
        self.line_numbers.defaultAction().setCheckable(True)
        self.line_numbers.defaultAction().setChecked(True)
        self.line_numbers.defaultAction().toggled.connect(self.toggle_line_numbers)
        self._find_shortcut = QShortcut(QKeySequence.StandardKey.Find, self)
        self._find_shortcut.activated.connect(self.search.setFocus)
        self.copy_button = self._command(self.tr("复制正文"), Icons.COPY, self.copy_preview)
        self.export_button = self._command(self.tr("另存为"), Icons.DOWNLOAD, self.save_as)
        self.more = self._command(self.tr("更多操作"), Icons.MORE)
        self.menu = prepare_context_menu(QMenu(self.more), self)
        self.more.setMenu(self.menu)
        self.more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu.aboutToShow.connect(self._populate_menu)
        self.reload_action = QAction(Icons.get_muted(Icons.REFRESH), self.tr("重新读取"), self)
        self.reload_action.triggered.connect(self.reload_current)
        self._overflow = []
        self.source_mode.defaultAction().toggled.connect(lambda _: self._sync_commands())
        self._close_shortcut = QShortcut(QKeySequence("Ctrl+W"), self)
        self._close_shortcut.activated.connect(self.close)
        self.editor.textChanged.connect(self._sync_title)
        self.viewer.system_open_requested.connect(self.open_system)
        self.viewer.reveal_requested.connect(self.reveal)
        self.viewer.attach_requested.connect(self.add_to_main_chat)
        self._set_editing(False)
        self.set_status("")
        jobs = self
        self.destroyed.connect(lambda: jobs.dispose())
        if target is not None:
            self.open_request({"target": target})

    @property
    def dirty(self):
        return self._editing and self.editor.document().isModified()

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
        title = self._page.get('title', self.tr('内容')) + (' *' if self.dirty else '')
        self.setWindowTitle(title)

    def toggle_line_numbers(self, enabled):
        self.editor.set_line_numbers_visible(enabled)
        if enabled and self.viewer.kind in {'markdown', 'html'} and not self._editing:
            self.source_mode.setChecked(True)

    def _sync_commands(self):
        ready = not self.loading and not self.busy
        self.viewer.overview_open.setEnabled(ready and self.target is not None)
        self.viewer.overview_reveal.setEnabled(ready and self.target is not None)
        self.viewer.overview_chat.setEnabled(ready and self.target is not None and self.receivers(self.attach_to_chat) > 0)
        self.viewer.picture.setEnabled(ready)
        if self.chat:
            self.chat.annotation_panel.setEnabled(ready)
        reading = not self._editing
        pdf = reading and self.viewer.kind == "pdf"
        raster = reading and self.viewer.kind in {"image", "pdf"}
        (self.source_mode if self.viewer.source_visible else self.read_mode).setChecked(True)
        with QSignalBlocker(self.line_numbers.defaultAction()):
            self.line_numbers.setChecked(self.editor.line_numbers_visible and
                                         (self._editing or self.viewer.kind == 'text' or self.viewer.source_visible))
        eligible = {
            self.back: bool(self._history),
            self.read_mode: reading and self.viewer.kind in {'markdown', 'html'},
            self.source_mode: reading and self.viewer.kind in {'markdown', 'html'},
            self.previous: pdf, self.counter: pdf, self.next: pdf,
            self.zoom_out: raster, self.zoom_reset: raster, self.zoom_in: raster, self.zoom_fit: raster,
            self.edit: reading and bool(self._page.get('writable')) and not self._request.get("evidence"),
            self.promote: reading and self._page.get("kind") == "artifact" and not self._request.get("evidence")
                and self.conversation is not None and has_active_workspace(self.conversation.work_dir),
            self.save: self._editing, self.cancel: self._editing,
            self.source_button: reading and bool(self._page.get("sources")),
            self.copy_button: bool(self._page),
            self.export_button: bool(self.target or self._request.get("image_source")), self.more: True,
            self.wrap: self.viewer.kind in {'text', 'markdown', 'html'} or self._editing,
            self.line_numbers: self.viewer.kind in {'text', 'markdown', 'html'} or self._editing,
            self.search: self.viewer.kind in {'text', 'markdown', 'html'} or self._editing,
            self.favorite: self.target is not None and self.services is not None and hasattr(self.services, 'library_service'),
            self.chat_toggle: self.chat is not None and self.viewer.kind in {'text', 'markdown', 'html', 'image'},
            self.annotation: reading and self.viewer.kind == 'image' and self.target is not None and self.services is not None and hasattr(self.services, 'library_service'),
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
        copy_label = {"image": self.tr("复制原图"), "pdf": self.tr("复制当前页"), "file": self.tr("复制位置")}.get(self.viewer.kind, self.tr("复制正文")) if reading else self.tr("复制正文")
        self.copy_button.defaultAction().setText(copy_label)
        self.copy_button.setToolTip(copy_label)
        self.copy_button.setAccessibleName(copy_label)
        self.reload_action.setEnabled(ready and bool(self._request))
        self.more.defaultAction().setEnabled(True)
        self._overflow = []
        # A single small row normally fits at 360 px; large system fonts may need overflow.
        available = max(0, self.width() - 12)
        self.search.setFixedWidth(max(90, min(172, available // 4)))
        def required_width():
            controls = [control for control in eligible if not control.isHidden()]
            return sum(control.sizeHint().width() if control is self.counter else control.width() for control in controls) + 4 * len(controls)
        for control in (self.export_button, self.source_button, self.promote, self.edit, self.copy_button,
                        self.chat_toggle, self.favorite, self.zoom_fit, self.zoom_reset):
            if required_width() <= available:
                break
            if eligible[control]:
                control.hide()
                self._overflow.append(control)

    def _sync_zoom(self, factor):
        self.zoom_reset.defaultAction().setText(f"{factor:.0%}")

    def _set_editing(self, enabled):
        self._editing = enabled
        self.editor.setReadOnly(not enabled)
        if enabled:
            self.viewer.stack.setCurrentWidget(self.editor)
        else:
            self.viewer._select_view()
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

    def matches_request(self, request, conversation):
        """Compare captured references/paths without reading or hashing files."""
        if not self._request or bool(request.get('evidence')) != bool(self._request.get('evidence')):
            return False
        if request.get('library_id'):
            return request.get('library_id') == self._request.get('library_id')
        def scope(conv):
            return (getattr(conv, 'id', ''), getattr(conv, 'work_dir', ''))
        if scope(conversation) != scope(self.conversation):
            return False
        if request == self._request:
            return True
        if self.target is None or request.get('change') != self._request.get('change'):
            return False
        incoming = request.get('target')
        ref = incoming.ref if incoming else request.get('ref')
        if isinstance(ref, dict):
            ref = ContentRef.from_dict(ref)
        if isinstance(ref, ContentRef):
            context = dict(work_dir=getattr(conversation, 'work_dir', ''), conversation_id=getattr(conversation, 'id', ''))
            return content_identity(ref, **context) == content_identity(self.target.ref, **context)
        if request.get('workspace_path'):
            return self.target.ref.ref == 'workspace:' + request['workspace_path']
        if request.get('path') and self.target.path is not None:
            path = Path(request['path']).expanduser()
            if not path.is_absolute():
                path = Path(getattr(conversation, 'work_dir', '') or os.getcwd()) / path
            return os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(self.target.path))
        return False

    def open_request(self, request, conversation=None, *, history=False, edit=False):
        request = dict(request)
        if not self.allow_leave(lambda: self.open_request(request, conversation, history=history, edit=edit)):
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
        self._load(edit=edit)
        self.show_window()

    def show_window(self):
        if self.isMinimized():
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()

    def open_memory(self, conversation, scope, entry_id=""):
        self.open_request({"memory": scope, "entry_id": entry_id}, conversation)

    def reload_current(self):
        if self.allow_leave(self.reload_current):
            self._set_editing(False)
            self._load(self.viewer.position(), latest=True)

    def _load(self, position=0, *, latest=False, keep_page=False, edit=False):
        if not keep_page:
            self._save_reading_position()
        self.abandon_reads()
        generation = self._generation
        request, conversation, services = dict(self._request), self.conversation, self.services
        request["latest"] = latest
        self.loading = True
        self.target, self._page = None, {}
        self._content_key = ''
        self._library_item = None
        self.favorite.setChecked(False)
        self.annotation.setChecked(False)
        if self.chat:
            self.chat.bind('', '')
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
            self.conversation = self._page.get('origin_conversation', self.conversation)
            if latest and self.target and self.target.ref.kind == "wiki":
                self._request = {"ref": self.target.ref, "evidence": bool(request.get("evidence"))}
            self.setWindowTitle(self._page["title"])
            self.set_status("\n".join(part for part in (self._page.get("status", ""), preview.notice) if part))
            self.viewer.apply(preview, target=self.target)
            self._content_key = self._key_for_content()
            self._library_item = self._page.get('library_item')
            self.favorite.setChecked(bool(self._library_item and self._library_item.favorite))
            self.viewer.picture.set_annotations(self._page.get('annotations', []))
            self._saved_annotations = [dict(entry) for entry in self.viewer.picture.annotations]
            if self.chat:
                self.chat.annotation_panel.set_entries(self.viewer.picture.annotations)
                self.chat.annotation_panel.setVisible(self.viewer.kind == 'image')
                supported = self.viewer.kind in {'text', 'markdown', 'html', 'image'}
                self.chat.bind(self._content_key if supported else '', self._page['title'], source=self.conversation)
                self._layout_chat()
            self.wrap.setChecked(self.editor.lineWrapMode() == self.editor.LineWrapMode.WidgetWidth)
            reading = self._page.get('reading') or {}
            self.editor.set_line_numbers_visible(bool(reading.get('line_numbers', True)))
            if reading:
                self.source_mode.setChecked(bool(reading.get('source')))
                self.wrap.setChecked(bool(reading.get('wrap', True)))
            self.viewer.restore_position(position or reading.get('position', 0))
            self.source_menu.clear()
            sources = self._page.get("sources", [])
            self.source_menu.setTitle(f"来源（{len(sources)}）")
            self.source_button.setToolTip(f"查看来源（{len(sources)}）")
            for index, source in enumerate(sources):
                label = source.get("name") or source.get("id") or "来源"
                action = self.source_menu.addAction(f"{index + 1}. {label}")
                action.triggered.connect(lambda _checked=False, ref=dict(source): self.open_source(ref))
            self._set_editing(False)
            if edit or (request.get("memory") and not request.get("entry_id") and self._page.get("writable")):
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
        if request.get('library_id'):
            item = services.library_service.get(request['library_id'])
            request = {**request, 'evidence': item.evidence}
            if item.ref.workspace or item.ref.conversation_id:
                origin = services.conv_service.load(item.ref.conversation_id) if item.ref.conversation_id else None
                conversation = origin or Conversation(id=item.ref.conversation_id, work_dir=item.ref.workspace)
            resolved = services.library_service.resolve(request['library_id'])
            target = replace(ContentOpenUseCase.classify_local_file(resolved.path), ref=resolved.ref)
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
        target = replace(target, ref=replace(target.ref, mime=target.mime))
        if target.is_image:
            target = replace(target, ref=replace(target.ref, digest=SessionContentResolver._digest(target.path)))
        if target.ref.kind == "archive" and target.ref.locator and services is not None:
            preview = PreparedPreview("text", text=services.knowledge_service.source_fragment(target.ref),
                                      notice=f"来源片段 · {target.ref.locator}")
        else:
            preview = ContentOpenUseCase.prepare_preview(target, page=request.get("pdf_page", 1))
        page = {"title": target.name, "kind": target.ref.kind, "status": "原始来源内容" if request.get("evidence") else ""}
        page['evidence'] = bool(request.get('evidence'))
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
        page['evidence'] = bool(request.get('evidence'))
        page['document'] = preview.document
        if request.get('library_id'):
            page['origin_conversation'] = conversation
        page['complete'] = preview.complete
        if services is not None and hasattr(services, 'library_service'):
            page['library_item'] = services.library_service.get(request['library_id']) if request.get('library_id') else services.library_service.find(target.ref, evidence=bool(request.get('evidence')))
            if page['library_item']:
                page['reading'] = services.library_service.view_state('reading:' + page['library_item'].id)
            if target.is_image:
                page['annotations'] = services.library_service.annotations(page['library_item'].id, target.ref.digest) if page['library_item'] else []
        if target.ref.kind in {'file', 'workspace', 'library'} and preview.document is not None:
            from pycat.models.workspace import WorkspaceLocation
            remote = bool(target.ref.workspace and WorkspaceLocation.parse(target.ref.workspace).is_remote)
            page['writable'] = bool(services is not None and getattr(services, 'document_service', None)
                                    and preview.document.editable and not remote and not request.get('evidence'))
        return target, preview, page

    def start_edit(self):
        if self.loading or self.busy or not self._page.get("writable"):
            return
        self._initial_text = self._page.get("body", "")
        self.editor.setPlainText(self._initial_text)
        self.editor.document().setModified(False)
        self._set_editing(True)
        self.editor.setFocus()

    def cancel_edit(self):
        self.editor.setPlainText(self._initial_text)
        self.editor.document().setModified(False)
        self._set_editing(False)

    def _key_for_content(self):
        if self.target is not None:
            identity = self.services.library_service.identity(self.target.ref) if self.services and hasattr(self.services, 'library_service') else str(self.target.path)
            if self._page.get('evidence'):
                identity += ':evidence:' + self.target.ref.digest
            return 'content:' + hashlib.sha256(identity.encode()).hexdigest()
        if self._request.get('memory'):
            return 'memory:' + str(getattr(self.conversation, 'work_dir', '')) + ':' + self._request['memory'] + ':' + self._request.get('entry_id', 'new')
        if self._request.get('image_source'):
            return 'image:' + hashlib.sha256(self._request['image_source'].encode()).hexdigest()
        return ''

    def capture_content(self):
        if self.loading or self.busy or not self._page or not self._content_key:
            raise ValueError(self.tr('内容尚未准备好'))
        if self.viewer.kind not in {'text', 'markdown', 'html', 'image'}:
            raise ValueError(self.tr('当前内容对话支持文本和图片。'))
        document = self._page.get('document')
        text = self.editor.toPlainText() if self.viewer.kind in {'text', 'markdown', 'html'} else ''
        cursor = self.editor.textCursor()
        source_visible = self._editing or self.viewer.stack.currentWidget() is self.editor
        selected = (utf16_to_python(text, cursor.selectionStart()), utf16_to_python(text, cursor.selectionEnd())) if source_visible and cursor.hasSelection() else None
        apply_range = selected if selected and selected[1] - selected[0] <= 12000 else None
        if apply_range is None and not selected and len(text) <= 12000 and self._page.get('writable') and (self._editing or source_visible):
            apply_range = (0, len(text))
        if not self._page.get('writable'):
            apply_range = None
        version = (document.digest if document else self._page.get('digest', '') or (self.target.ref.digest if self.target else ''))
        return {'key': self._content_key, 'name': self._page['title'], 'generation': self._generation,
                'version': version + ':draft:' + str(self.editor.document().revision()),
                'revision': self.editor.document().revision(), 'text': text,
                'selection': selected, 'apply_range': apply_range,
                'selected_text': text[slice(*apply_range)] if apply_range is not None else '',
                'image_path': (str(self.target.path) if self.target else self._request.get('image_source', '')) if self.viewer.kind == 'image' else '',
                'image_digest': self.target.ref.digest if self.target and self.viewer.kind == 'image' else '',
                'annotations': self.viewer.picture.annotations if self.viewer.kind == 'image' else [],
                'complete': self._page.get('complete', True)}

    def apply_suggestion(self, capture, replacement):
        if capture['key'] != self._content_key or capture['generation'] != self._generation or capture['revision'] != self.editor.document().revision():
            self.set_status(self.tr('内容或草稿已变化，请重新选择后生成建议。'))
            return
        if capture.get('selection') and capture['selection'] != (
            utf16_to_python(self.editor.toPlainText(), self.editor.textCursor().selectionStart()),
            utf16_to_python(self.editor.toPlainText(), self.editor.textCursor().selectionEnd())):
            self.set_status(self.tr('选区已变化，请重新生成建议。'))
            return
        if capture.get('apply_range') is None or not self._page.get('writable'):
            return
        text, (start, end) = self.editor.toPlainText(), capture['apply_range']
        if text[start:end] != capture['selected_text']:
            self.set_status(self.tr('选中内容已变化，建议未应用。'))
            return
        # Enter editing without resetting the document or changing the captured selection.
        self._initial_text = self._page.get('body', '')
        self._set_editing(True)
        cursor = self.editor.textCursor()
        cursor.setPosition(len(text[:start].encode('utf-16-le')) // 2)
        cursor.setPosition(len(text[:end].encode('utf-16-le')) // 2, QTextCursor.MoveMode.KeepAnchor)
        cursor.beginEditBlock()
        cursor.insertText(replacement)
        cursor.endEditBlock()
        self.editor.setTextCursor(cursor)
        self.editor.setFocus()
        self.set_status(self.tr('建议已应用到草稿；可撤销，保存后才会写入来源。'))

    def _quote_selection(self, text):
        if self.chat:
            self._chat_visible = True
            self._chat_force = True
            self._layout_chat()
            self.chat.set_quote(text)

    def toggle_chat(self):
        if self.chat:
            self._chat_visible = not self.chat.isVisible()
            self._chat_force = self._chat_visible
            self._layout_chat()

    def _layout_chat(self):
        if self.chat:
            narrow = self.width() < 760
            self.body.setOrientation(Qt.Orientation.Vertical if narrow else Qt.Orientation.Horizontal)
            self.chat.setMinimumWidth(0 if narrow else 240)
            visible = self._chat_visible and self.viewer.kind in {'text', 'markdown', 'html', 'image'} and (not narrow or self._chat_force)
            self.chat.setVisible(visible)
            self.chat_toggle.setChecked(visible)
            label = self.tr('关闭内容对话') if visible else self.tr('打开内容对话')
            self.chat_toggle.setToolTip(label)
            self.chat_toggle.setAccessibleName(label)

    def toggle_favorite(self):
        if self.target is None:
            return
        ref, library, evidence = self.target.ref, self.services.library_service, bool(self._page.get('evidence'))
        def operation():
            item = library.add_reference(ref, evidence=evidence)
            return library.set_favorite(item.id, not item.favorite)
        def done(item):
            self._library_item = item
            self.favorite.setChecked(item.favorite)
            self.changed.emit()
        self._mutate(operation, done)

    def save_annotations(self, annotations):
        if self.target is None or not self.services:
            return
        target, library, entries = self.target, self.services.library_service, [dict(entry) for entry in annotations]
        current, evidence = self._library_item, bool(self._page.get('evidence'))
        def operation():
            item = current or library.add_reference(target.ref, evidence=evidence)
            library.set_annotations(item.id, target.ref.digest, entries)
            return item
        def done(item):
            self._library_item = item
            self._saved_annotations = entries
            self.changed.emit()
        def failed():
            self.viewer.picture.set_annotations(self._saved_annotations)
            if self.chat:
                self.chat.annotation_panel.set_entries(self._saved_annotations)
        self._mutate(operation, done, on_error=failed)

    def annotations_changed(self, entries):
        if self.chat:
            self.chat.annotation_panel.set_entries(entries, self.viewer.picture.selected_annotation)
        self.save_annotations(entries)

    def select_annotation(self, index):
        if self.chat:
            self.chat.annotation_panel.select(index)
            if index >= 0:
                self._chat_visible = self._chat_force = True
                self._layout_chat()

    def focus_annotation_note(self, index):
        self.select_annotation(index)
        if self.chat:
            self.chat.annotation_panel.note.setFocus()

    def _mutate(self, operation, finished, *, on_discard=None, on_error=None):
        if self.busy:
            return
        self.busy = True
        self._sync_commands()
        job = BackgroundJob(operation, on_discard=on_discard)
        generation = self._generation
        self._write_job = job
        def complete(result, error):
            if self._write_job is not job:
                return
            self._write_job = None
            self.busy = False
            self._sync_commands()
            if generation != self._generation:
                if on_discard:
                    on_discard(result, error)
                return
            if error:
                self.set_status(str(error))
                if on_error:
                    on_error()
            else:
                finished(result)
        job.signals.finished.connect(complete)
        QThreadPool.globalInstance().start(job)

    def save_changes(self, on_success=None):
        if not self._editing or not self._page.get("writable"):
            return
        page, request, conv = dict(self._page), dict(self._request), self.conversation
        text = self.editor.toPlainText()
        def operation():
            if page['kind'] in {'file', 'workspace', 'library'}:
                self.services.document_service.save(page['document'], text)
                if request.get('library_id'):
                    self.services.library_service.refresh_owned(request['library_id'])
                    return request
                return {'path': str(page['document'].path)}
            service = self.services.knowledge_service
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
        if self.viewer.kind == 'image' and self.viewer.picture.annotations:
            menu.addAction(self.tr('清除图片标注'), self.clear_annotations)
        if self.target:
            menu.addAction(self.tr('加入主对话'), self.add_to_main_chat).setEnabled(not self.loading and not self.busy)
            menu.addAction("系统打开", self.open_system).setEnabled(not self.loading and not self.busy)
            menu.addAction("显示所在文件夹", self.reveal).setEnabled(not self.loading and not self.busy)
        if self._page.get('kind') in {'wiki', 'memory'} and self._page.get("writable") and (not self._request.get("memory") or self._request.get("entry_id")):
            menu.addSeparator()
            label = "忘记这条记忆" if self._request.get("memory") else "删除知识"
            menu.addAction(label, self._confirm_delete).setEnabled(not self.busy)

    def _confirm_delete(self):
        label = "忘记这条记忆" if self._request.get("memory") else "删除这条知识"
        if QMessageBox.question(self, label, f"确定{label}？") == QMessageBox.StandardButton.Yes:
            self.delete_current()

    def clear_annotations(self):
        if self.busy:
            return
        self.viewer.picture.set_annotations([])
        self.annotations_changed([])

    def add_to_main_chat(self):
        if self.target is not None:
            target = self.target
            self._mutate(lambda: ContentOpenUseCase.verify_target(target),
                         lambda _: self.attach_to_chat.emit(str(target.path)))

    def _save_reading_position(self):
        if self._library_item is not None and self.services is not None:
            library, key = self.services.library_service, 'reading:' + self._library_item.id
            value = {'position': self.viewer.position(), 'source': self.viewer.source_visible,
                     'wrap': self.editor.lineWrapMode() == self.editor.LineWrapMode.WidgetWidth,
                     'line_numbers': self.editor.line_numbers_visible}
            job = BackgroundJob(lambda: library.save_view_state(key, value))
            QThreadPool.globalInstance().start(job)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "reload_action"):
            self._sync_commands()
            self._layout_chat()

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
        elif self.viewer.kind == 'file' and self.target:
            QGuiApplication.clipboard().setText(self.viewer.file_path.text())
        else:
            QGuiApplication.clipboard().setText(self.editor.toPlainText() if self._editing else self.viewer.text)

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
            text = self.editor.toPlainText() if self._editing else None
            document = self._page.get('document')
            self._mutate(lambda: ContentOpenUseCase.prepare_save_as(target, path, format=format, text=text, document=document), ContentOpenUseCase.commit_save_as,
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
        if self._disposed:
            return
        self._disposed = True
        if not sip.isdeleted(self):
            self._save_reading_position()
        self.abandon_reads()
        if self.chat:
            self.chat.dispose()
        if self._write_job:
            self._write_job.abandon()
            self._write_job = None
        if not sip.isdeleted(self):
            self.hide()
            self.deleteLater()

    def closeEvent(self, event):
        if not self.allow_leave(self.close):
            event.ignore()
            return
        self._save_reading_position()
        self.abandon_reads()
        if self.chat:
            self.chat.bind('', '')
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
        # QDialog.closeEvent calls reject(), whose override routes through the
        # draft guard above. Finish explicitly so owners release this instance.
        super().reject()
        event.accept()
        focus = self._return_focus() if self._return_focus else None
        self._return_focus = None
        if focus is not None and not sip.isdeleted(focus):
            focus.setFocus()

    def reject(self):
        self.close()
