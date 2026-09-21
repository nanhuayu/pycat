"""Painted project/conversation navigation over repository summaries."""
from __future__ import annotations
import os
from pathlib import Path
from PyQt6.QtCore import QEvent, QRect, QSize, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QFont, QPainter, QColor
from PyQt6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QInputDialog, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QPushButton, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget)
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.display_text import single_line
from pycat.gui.utils.theme import prepare_context_menu, resolve_accent, resolve_theme, theme_colors
from pycat.gui.widgets.themed_line_edit import SearchLineEdit
from pycat.core.content.export import CONVERSATION_FORMATS
from pycat.models.workspace import WorkspaceLocation, workspace_identity

TITLE_ROLE = Qt.ItemDataRole.UserRole + 1
META_ROLE = Qt.ItemDataRole.UserRole + 2
HAS_WORK_DIR_ROLE = Qt.ItemDataRole.UserRole + 3
RUNNING_ROLE = Qt.ItemDataRole.UserRole + 4
KIND_ROLE = Qt.ItemDataRole.UserRole + 5
EXPANDED_ROLE = Qt.ItemDataRole.UserRole + 6
WAITING_ROLE = Qt.ItemDataRole.UserRole + 7


def project_key(path: str) -> str:
    """No disk scan: identity is the full normalized directory, never its name."""
    return workspace_identity(path)


class ConversationItem(QListWidgetItem):
    def __init__(self, data: dict):
        super().__init__()
        self.conversation_data = data
        self.title = str(data.get("title") or "无标题")
        self.work_dir = str(data.get("work_dir") or "")
        self.setText(self.title)
        self.setData(TITLE_ROLE, self.title)
        self.setData(KIND_ROLE, "conversation")
        self.setData(META_ROLE, "")
        self.setData(HAS_WORK_DIR_ROLE, bool(self.work_dir))
        self.setData(RUNNING_ROLE, False)
        self.setSizeHint(QSize(0, 36))
        self._base_tooltip = f"{self.title}\n{self.work_dir or '个人空间'}\nSession ID: {data.get('id', '')}"
        if data.get("archived"):
            self._base_tooltip += "\n已归档"
        self.setToolTip(self._base_tooltip)

    def set_runtime_state(self, streaming, waiting=False):
        self.setData(RUNNING_ROLE, bool(streaming))
        self.setData(WAITING_ROLE, bool(waiting))
        status = "等待你的回复" if waiting else "正在生成" if streaming else ""
        self.setToolTip(self._base_tooltip + (f"\n状态: {status}" if status else ""))
        self.setData(Qt.ItemDataRole.AccessibleTextRole, f"{self.title}，{status}" if status else self.title)

    def matches_search(self, query):
        query = query.strip().casefold()
        self.setData(META_ROLE, "")
        metadata = " ".join(str(self.conversation_data.get(k) or "") for k in ("title", "work_dir", "model", "provider_name"))
        if not query or query in metadata.casefold():
            return True
        for entry in reversed(self.conversation_data.get("search_entries") or []):
            text = str(entry.get("text") or "")
            index = text.casefold().find(query)
            if index >= 0:
                role = "你" if entry.get("role") == "user" else "助手"
                self.setData(META_ROLE, f"{role}：{text[max(0, index - 18):index + len(query) + 54]}")
                return True
        return False


class ProjectItem(QListWidgetItem):
    def __init__(self, path, *, expanded, pinned):
        super().__init__(WorkspaceLocation.parse(path).label or path)
        self.path, self.key, self.pinned = path, project_key(path), pinned
        self.setData(TITLE_ROLE, self.text())
        self.setData(KIND_ROLE, "project")
        self.setData(EXPANDED_ROLE, expanded)
        self.setToolTip(path)
        self.setSizeHint(QSize(0, 36))


class ConversationItemDelegate(QStyledItemDelegate):
    """Row actions are painted too; no QWidget per conversation."""
    def sizeHint(self, option, index):
        return QSize(0, 48 if index.data(META_ROLE) else 36)

    def paint(self, painter: QPainter, option, index):
        colors = theme_colors(resolve_theme(self.parent()), resolve_accent(self.parent()))
        rect = option.rect.adjusted(4, 2, -4, -2)
        kind = index.data(KIND_ROLE)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & (QStyle.StateFlag.State_MouseOver | QStyle.StateFlag.State_HasFocus))
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        if (selected and kind == "conversation") or hovered:
            painter.setBrush(QColor(colors["selected"] if selected else colors["surface_hover"]))
            painter.drawRoundedRect(rect, 6, 6)
        color = colors["selected_text"] if selected else colors["text"]
        painter.setPen(QColor(colors["muted"] if kind == "section" else color))
        font = QFont(option.font)
        if kind == "section":
            font.setPointSize(max(8, font.pointSize() - 1))
        painter.setFont(font)
        left = rect.left() + (26 if kind == "conversation" and index.data(HAS_WORK_DIR_ROLE) else 6)
        if kind != "section":
            icon = Icons.FOLDER if kind == "project" else Icons.FILE
            Icons.get(icon, color=colors["muted"]).paint(painter, QRect(left, rect.top() + 8, 16, 16))
            left += 22
        waiting = kind == "conversation" and bool(index.data(WAITING_ROLE))
        reserved = 78 if waiting else 54 if hovered and kind == "project" else 28
        text_rect = QRect(left, rect.top(), max(1, rect.right() - left - reserved), 32)
        title = painter.fontMetrics().elidedText(single_line(index.data(TITLE_ROLE)), Qt.TextElideMode.ElideRight, text_rect.width())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter, title)
        if meta := index.data(META_ROLE):
            painter.setPen(QColor(colors["muted"]))
            painter.drawText(QRect(left, rect.top() + 23, text_rect.width(), 20), Qt.AlignmentFlag.AlignVCenter,
                            painter.fontMetrics().elidedText(single_line(meta), Qt.TextElideMode.ElideRight, text_rect.width()))
        action_rect = QRect(rect.right() - 22, rect.top() + 8, 16, 16)
        if waiting:
            painter.setPen(QColor(colors["primary"]))
            painter.drawText(QRect(rect.right() - 76, rect.top(), 50, 32), Qt.AlignmentFlag.AlignVCenter, "待回复")
        if kind == "section":
            Icons.get_muted(Icons.PLUS).paint(painter, action_rect)
        elif hovered:
            Icons.get_muted(Icons.MORE).paint(painter, action_rect)
            if kind == "project":
                Icons.get_muted(Icons.PLUS).paint(painter, action_rect.translated(-26, 0))
        elif kind == "project":
            Icons.get_muted(Icons.CHEVRON_DOWN if index.data(EXPANDED_ROLE) else Icons.CHEVRON_RIGHT).paint(painter, action_rect)
        elif index.data(RUNNING_ROLE) and not waiting:
            painter.setBrush(QColor(colors["primary"]))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(action_rect.adjusted(5, 5, -5, -5))
        painter.restore()


class Sidebar(QWidget):
    conversation_selected = pyqtSignal(str)
    new_conversation = pyqtSignal()
    new_in_project = pyqtSignal(str)
    project_requested = pyqtSignal()
    import_conversation = pyqtSignal(str)
    delete_conversation = pyqtSignal(str)
    export_conversation = pyqtSignal(str, str)
    navigation_requested = pyqtSignal(str, dict)
    preferences_changed = pyqtSignal(dict)
    settings_requested = pyqtSignal()
    materials_requested = pyqtSignal()
    about_requested = pyqtSignal()
    search_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumWidth(180)
        self.setMaximumWidth(320)
        self._all_conversations = []
        self._streaming_conversation_ids = set()
        self._waiting_conversation_ids = set()
        self._projects = {"pinned": [], "hidden": [], "collapsed": []}
        self._show_archived = False
        self.collapsed = False
        self.expanded_width = 212
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(8)
        self.brand_row = QWidget()
        self.brand_row.setFixedHeight(40)
        brand = QHBoxLayout(self.brand_row)
        brand.setContentsMargins(0, 0, 0, 0)
        brand.setSpacing(6)
        self.brand_layout = brand
        self.brand_btn = QPushButton("PyCat")
        self.brand_btn.setObjectName("brand_btn")
        self.brand_btn.setIcon(Icons.brand())
        self.brand_btn.setIconSize(QSize(28, 28))
        self.brand_btn.setToolTip("关于 PyCat")
        self.brand_btn.setAccessibleName("关于 PyCat")
        self.brand_btn.clicked.connect(self.about_requested)
        brand.addWidget(self.brand_btn)
        brand.addStretch()
        self.new_chat_btn = QPushButton()
        self.new_chat_btn.setObjectName("new_chat_btn")
        self.new_chat_btn.setIcon(Icons.get(Icons.PLUS))
        self.new_chat_btn.setFixedSize(30, 30)
        self.new_chat_btn.setToolTip("新建对话 · Ctrl+N")
        self.new_chat_btn.setAccessibleName("新建对话")
        self.new_chat_btn.clicked.connect(self.new_conversation)
        brand.addWidget(self.new_chat_btn)
        layout.addWidget(self.brand_row)
        self.search_btn = QPushButton()
        self.search_btn.setObjectName("sidebar_search_btn")
        self.search_btn.setIcon(Icons.get_muted(Icons.SEARCH))
        self.search_btn.setFixedSize(36, 34)
        self.search_btn.setToolTip("搜索对话")
        self.search_btn.setAccessibleName("搜索对话")
        self.search_btn.clicked.connect(self.search_requested)
        self.search_btn.hide()
        layout.addWidget(self.search_btn)
        self.search_input = SearchLineEdit()
        self.search_input.setObjectName("search_input")
        self.search_input.setPlaceholderText("搜索对话")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setFixedHeight(34)
        self.search_input.textChanged.connect(self._filter_conversations)
        layout.addWidget(self.search_input)
        self.conversation_list = QListWidget()
        self.conversation_list.setObjectName("conversation_list")
        self.conversation_list.setItemDelegate(ConversationItemDelegate(self.conversation_list))
        self.conversation_list.setMouseTracking(True)
        self.conversation_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.conversation_list.itemClicked.connect(self._on_item_clicked)
        self.conversation_list.itemActivated.connect(self._on_item_clicked)
        self.conversation_list.viewport().installEventFilter(self)
        self.conversation_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.conversation_list.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self.conversation_list, 1)
        layout.addStretch(0)
        self._rail_spacer_index = layout.count() - 1
        for label, icon, signal in (("资料与记忆", Icons.BOOK, self.materials_requested), ("设置", Icons.SETTINGS, self.settings_requested)):
            button = QPushButton(label)
            button.setIcon(Icons.get_muted(icon))
            button.setProperty("icon_name", icon)
            button.setObjectName("sidebar_footer_btn")
            button.setProperty("label", label)
            button.setToolTip(label)
            button.setAccessibleName(label)
            button.clicked.connect(signal)
            layout.addWidget(button)

    def set_collapsed(self, collapsed: bool):
        collapsed = bool(collapsed)
        if self.collapsed == collapsed:
            return
        if collapsed:
            self.expanded_width = max(180, min(320, self.width()))
        self.collapsed = collapsed
        self.setMinimumWidth(56 if collapsed else 180)
        self.setMaximumWidth(56 if collapsed else 320)
        self.brand_layout.setDirection(QHBoxLayout.Direction.TopToBottom if collapsed else QHBoxLayout.Direction.LeftToRight)
        self.brand_row.setFixedHeight(80 if collapsed else 40)
        self.brand_btn.setText("" if collapsed else "PyCat")
        self.search_input.setVisible(not collapsed)
        self.search_btn.setVisible(collapsed)
        self.conversation_list.setVisible(not collapsed)
        self.layout().setStretch(self._rail_spacer_index, 1 if collapsed else 0)
        for button in self.findChildren(QPushButton, "sidebar_footer_btn"):
            button.setText("" if collapsed else button.property("label"))

    def refresh_theme(self):
        self.new_chat_btn.setIcon(Icons.get(Icons.PLUS))
        self.search_btn.setIcon(Icons.get_muted(Icons.SEARCH))
        for button in self.findChildren(QPushButton, "sidebar_footer_btn"):
            button.setIcon(Icons.get_muted(button.property("icon_name")))
        self.conversation_list.viewport().update()

    def show_active(self):
        self._show_archived = False
        self.search_input.clear()
        self._filter_conversations("")

    def apply_navigation_settings(self, settings):
        values = settings.get("project_navigation") or {}
        self._projects = {k: [project_key(str(p)) for p in values.get(k, []) if p] for k in self._projects}
        self._filter_conversations(self.search_input.text())

    def navigation_settings(self):
        return {"project_navigation": {k: list(v) for k, v in self._projects.items()}}

    def _project_preference(self, key, path, enabled):
        values = set(self._projects[key])
        values.add(project_key(path)) if enabled else values.discard(project_key(path))
        self._projects[key] = sorted(values)
        self.preferences_changed.emit(self.navigation_settings())
        self._filter_conversations(self.search_input.text())

    def set_project_hidden(self, path, hidden):
        self._project_preference("hidden", path, hidden)

    def update_conversations(self, conversations):
        self._all_conversations = conversations
        self._filter_conversations(self.search_input.text())

    def _section(self, title):
        item = QListWidgetItem(title)
        item.setData(TITLE_ROLE, title)
        item.setData(KIND_ROLE, "section")
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self.conversation_list.addItem(item)

    def _add_conversation(self, data, *, hidden=False):
        item = ConversationItem(data)
        item.set_runtime_state(data.get("id") in self._streaming_conversation_ids,
                               data.get("id") in self._waiting_conversation_ids)
        self.conversation_list.addItem(item)
        item.setHidden(hidden)
        return item

    def _filter_conversations(self, text):
        current = self.conversation_list.currentItem()
        current_id = current.conversation_data.get("id") if isinstance(current, ConversationItem) else None
        self.conversation_list.setUpdatesEnabled(False)
        self.conversation_list.clear()
        if text.strip():
            for row in self._all_conversations:
                item = self._add_conversation(row)
                item.setHidden(not item.matches_search(text))
        else:
            rows = sorted((r for r in self._all_conversations if bool(r.get("archived")) == self._show_archived),
                          key=lambda r: (bool(r.get("pinned")), str(r.get("updated_at") or "")), reverse=True)
            groups, recent = {}, []
            for row in rows:
                path = project_key(str(row.get("work_dir") or ""))
                if not path or path in self._projects["hidden"] or self._show_archived:
                    recent.append(row)
                else:
                    groups.setdefault(path, []).append(row)
            self._section("项目")
            for path in sorted(groups, key=lambda p: (p not in self._projects["pinned"], p.casefold())):
                expanded = path not in self._projects["collapsed"]
                self.conversation_list.addItem(ProjectItem(path, expanded=expanded, pinned=path in self._projects["pinned"]))
                for row in groups[path]:
                    self._add_conversation(row, hidden=not expanded)
            self._section("已归档" if self._show_archived else "最近")
            for row in recent:
                self._add_conversation(row)
        if current_id:
            self.select_conversation(current_id, reveal=False)
        self.conversation_list.setUpdatesEnabled(True)

    def set_conversation_streaming(self, conversation_id, streaming):
        self._streaming_conversation_ids.add(conversation_id) if streaming else self._streaming_conversation_ids.discard(conversation_id)
        self.set_streaming_conversations(self._streaming_conversation_ids)

    def set_streaming_conversations(self, conversation_ids):
        self._streaming_conversation_ids = set(conversation_ids or ())
        self._refresh_runtime_states()

    def set_waiting_conversations(self, conversation_ids):
        waiting = set(conversation_ids or ())
        if waiting != self._waiting_conversation_ids:
            self._waiting_conversation_ids = waiting
            self._refresh_runtime_states()

    def _refresh_runtime_states(self):
        for i in range(self.conversation_list.count()):
            item = self.conversation_list.item(i)
            if isinstance(item, ConversationItem):
                cid = item.conversation_data.get("id")
                item.set_runtime_state(cid in self._streaming_conversation_ids, cid in self._waiting_conversation_ids)
        self.conversation_list.viewport().update()

    def select_conversation(self, conversation_id, *, reveal=True):
        for i in range(self.conversation_list.count()):
            item = self.conversation_list.item(i)
            if isinstance(item, ConversationItem) and item.conversation_data.get("id") == conversation_id:
                if reveal and item.isHidden() and not self.search_input.text():
                    self._project_preference("collapsed", item.work_dir, False)
                    return self.select_conversation(conversation_id, reveal=False)
                self.conversation_list.setCurrentItem(item)
                break

    def _on_item_clicked(self, item):
        if isinstance(item, ConversationItem):
            self.conversation_selected.emit(str(item.conversation_data.get("id", "")))
        elif isinstance(item, ProjectItem):
            self._project_preference("collapsed", item.path, bool(item.data(EXPANDED_ROLE)))

    def eventFilter(self, watched, event):
        if watched == self.conversation_list.viewport() and event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            item = self.conversation_list.itemAt(pos)
            if item is not None:
                right = self.conversation_list.visualItemRect(item).right() - pos.x()
                if 0 <= right < 34:
                    if item.data(KIND_ROLE) == "section":
                        self.add_project() if item.text() == "项目" else self.new_in_project.emit("")
                    else:
                        self._show_context_menu(pos)
                    return True
                if isinstance(item, ProjectItem) and 34 <= right < 60:
                    self.new_in_project.emit(item.path)
                    return True
        return super().eventFilter(watched, event)

    def add_project(self):
        self.project_requested.emit()

    def _show_context_menu(self, position):
        item = self.conversation_list.itemAt(position) or self.conversation_list.currentItem()
        if isinstance(item, ConversationItem):
            menu = self._build_context_menu(item)
        elif isinstance(item, ProjectItem):
            menu = prepare_context_menu(QMenu(self), self)
            menu.addAction("新建对话", lambda: self.new_in_project.emit(item.path))
            menu.addAction("取消置顶" if item.pinned else "置顶项目", lambda: self._project_preference("pinned", item.path, not item.pinned))
            menu.addAction("复制路径", lambda: QApplication.clipboard().setText(item.path))
            menu.addAction("从侧边栏移除", lambda: self.set_project_hidden(item.path, True))
        else:
            menu = self.management_menu()
        try:
            menu.exec(self.conversation_list.viewport().mapToGlobal(position))
        finally:
            menu.deleteLater()

    def management_menu(self):
        menu = prepare_context_menu(QMenu(self), self)
        menu.addAction("新建个人对话", lambda: self.new_in_project.emit(""))
        menu.addAction("添加项目", self.add_project)
        menu.addAction("显示最近对话" if self._show_archived else "查看已归档", self._toggle_archived)
        menu.addAction("导入 JSON…", self.prompt_import_conversation)
        if self._projects["hidden"]:
            submenu = menu.addMenu("恢复项目显示")
            for path in self._projects["hidden"]:
                submenu.addAction(path, lambda checked=False, p=path: self.set_project_hidden(p, False))
        return menu

    def _toggle_archived(self):
        self._show_archived = not self._show_archived
        self.search_input.clear()
        self._filter_conversations("")

    def _build_context_menu(self, item):
        menu = prepare_context_menu(QMenu(self), self)
        cid = str(item.conversation_data.get("id") or "")
        menu.addAction("新建对话", lambda: self.new_in_project.emit(item.work_dir))
        for label, key in (("置顶", "pinned"), ("归档", "archived")):
            value = bool(item.conversation_data.get(key))
            title = ("取消置顶" if key == "pinned" else "恢复对话") if value else label
            action = menu.addAction(title, lambda checked=False, k=key, v=not value: self.navigation_requested.emit(cid, {k: v}))
            action.setEnabled(cid not in self._streaming_conversation_ids)
        menu.addAction("重命名", lambda: self._rename(cid, item.title))
        export = menu.addMenu("导出会话")
        for fmt, (_, label) in CONVERSATION_FORMATS.items():
            export.addAction(f"导出为 {label}…", lambda checked=False, f=fmt: self.export_conversation.emit(cid, f))
        menu.addAction("复制 Session ID", lambda: self._copy_session_id(cid))
        action = menu.addAction("在资源管理器中打开", lambda: self._open_work_dir(item.work_dir))
        action.setEnabled(bool(item.work_dir and Path(item.work_dir).is_dir()))
        menu.addSeparator()
        action = menu.addAction("删除", lambda: self._confirm_delete(cid))
        action.setEnabled(cid not in self._streaming_conversation_ids)
        return menu

    def _rename(self, cid, title):
        text, ok = QInputDialog.getText(self, "重命名对话", "名称", text=title)
        if ok and text.strip():
            self.navigation_requested.emit(cid, {"title": text.strip()})

    @staticmethod
    def _open_work_dir(work_dir):
        if work_dir and Path(work_dir).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(work_dir).resolve())))

    def _copy_session_id(self, conversation_id):
        QApplication.clipboard().setText(conversation_id)

    def _confirm_delete(self, conversation_id):
        if QMessageBox.question(self, "删除会话", "确定要删除这个会话吗？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.delete_conversation.emit(conversation_id)

    def prompt_import_conversation(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入会话", "", "JSON 文件 (*.json);;所有文件 (*)")
        if path:
            self.import_conversation.emit(path)
