"""
Sidebar widget for conversation list and management - Chinese UI
"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, 
    QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QMessageBox, QFileDialog, QApplication, QStyledItemDelegate, QStyle
)
from PyQt6.QtCore import pyqtSignal, Qt, QSize, QRect
from PyQt6.QtGui import QAction, QColor, QFont, QFontMetrics, QPainter, QPalette, QPen
from typing import Dict, Any, List
from datetime import datetime
import os

from gui.utils.icon_manager import Icons
from gui.utils.theme import prepare_context_menu, resolve_accent, resolve_theme, theme_colors
from gui.widgets.themed_line_edit import ThemedLineEdit


TITLE_ROLE = Qt.ItemDataRole.UserRole + 1
META_ROLE = Qt.ItemDataRole.UserRole + 2
HAS_WORK_DIR_ROLE = Qt.ItemDataRole.UserRole + 3
RUNNING_ROLE = Qt.ItemDataRole.UserRole + 4


class ConversationItem(QListWidgetItem):
    """Custom list item for conversations - compact display"""
    
    def __init__(self, data: Dict[str, Any]):
        super().__init__()
        self.conversation_data = data
        self.title = str(data.get('title', '无标题') or '无标题')
        self.model = str(data.get('model', '') or '')
        self.work_dir = str(data.get('work_dir', '') or '').strip()
        updated_at = data.get('updated_at') or data.get('created_at')
        self.updated_str = ""
        if isinstance(updated_at, str) and updated_at:
            try:
                dt = datetime.fromisoformat(updated_at)
                self.updated_str = dt.strftime('%m-%d %H:%M')
            except Exception:
                self.updated_str = ""

        count = data.get('message_count', 0)
        session_id = str(data.get('id', '') or '')
        self._base_tooltip = (
            f"标题: {self.title}\nSession ID: {session_id or '-'}\n"
            f"工作区: {self.work_dir or '-'}\n模型: {self.model or '未设置'}\n消息: {count} 条\n更新: {self.updated_str or '-'}"
        )
        self.setToolTip(self._base_tooltip)
        self.setSizeHint(QSize(0, 44))
        self._default_meta = self.meta_text()
        self.setData(TITLE_ROLE, self.title)
        self.setData(META_ROLE, self._default_meta)
        self.setData(HAS_WORK_DIR_ROLE, bool(self.work_dir))
        self.setData(RUNNING_ROLE, False)

    def set_streaming_state(self, streaming: bool) -> None:
        self.setData(RUNNING_ROLE, bool(streaming))
        suffix = "\n状态: 正在生成" if streaming else ""
        self.setToolTip(self._base_tooltip + suffix)

    def meta_text(self) -> str:
        if self.work_dir:
            name = os.path.basename(os.path.normpath(self.work_dir)) or self.work_dir
            return name
        if self.updated_str:
            return self.updated_str
        count = int(self.conversation_data.get('message_count', 0) or 0)
        return f"{count} 条" if count else ""

    def matches_search(self, query: str) -> bool:
        normalized = str(query or "").strip().casefold()
        self.setData(META_ROLE, self._default_meta)
        if not normalized:
            return True

        metadata = " ".join(
            str(self.conversation_data.get(key, "") or "")
            for key in ("title", "work_dir", "model", "provider_name")
        ).casefold()
        if normalized in metadata:
            return True

        entries = self.conversation_data.get("search_entries")
        if not isinstance(entries, list):
            return False
        for entry in reversed(entries):
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("text") or "").strip()
            index = text.casefold().find(normalized)
            if index < 0:
                continue
            role = "你" if str(entry.get("role") or "") == "user" else "助手"
            self.setData(META_ROLE, f"{role}：{self._search_snippet(text, index, len(normalized))}")
            return True
        return False

    @staticmethod
    def _search_snippet(text: str, index: int, query_length: int) -> str:
        start = max(0, index - 18)
        end = min(len(text), index + max(1, query_length) + 54)
        prefix = "..." if start else ""
        suffix = "..." if end < len(text) else ""
        return f"{prefix}{text[start:end]}{suffix}"


class ConversationItemDelegate(QStyledItemDelegate):
    """Paint conversation rows without creating one QWidget per row."""

    def __init__(self, parent=None):
        super().__init__(parent)

    def sizeHint(self, option, index):
        return QSize(0, 44)

    def paint(self, painter: QPainter, option, index):
        painter.save()
        rect = option.rect.adjusted(4, 1, -4, -1)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        owner = self.parent() if isinstance(self.parent(), QWidget) else None
        theme = resolve_theme(owner)
        colors = theme_colors(theme, resolve_accent(owner))
        is_dark = theme == "dark"
        window = QColor(colors["window"])
        text = QColor(colors["text"])
        muted = QColor(colors["muted"])
        highlighted_text = QColor(colors["selected_text"])

        if selected:
            bg = QColor(colors["selected"])
            title_color = highlighted_text if is_dark else QColor(colors["selected_text"])
            meta_color = QColor(colors["selected_meta"])
            border_color = QColor(colors["selected_border"])
        elif hovered:
            bg = QColor(colors["surface_hover"])
            title_color = text
            meta_color = muted
            border_color = QColor(colors["control_border"] if is_dark else colors["border"])
        else:
            bg = window
            title_color = text
            meta_color = muted
            border_color = window
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(rect, 7, 7)
        if selected or hovered:
            painter.setPen(QPen(border_color, 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(0, 0, -1, -1), 7, 7)

        title = str(index.data(TITLE_ROLE) or "")
        meta = str(index.data(META_ROLE) or "")
        has_work_dir = bool(index.data(HAS_WORK_DIR_ROLE))
        is_running = bool(index.data(RUNNING_ROLE))

        title_font = QFont(option.font)
        title_font.setPointSize(max(8, option.font.pointSize()))
        title_font.setWeight(QFont.Weight.DemiBold if selected else QFont.Weight.Medium)
        meta_font = QFont(option.font)
        meta_font.setPointSize(max(7, option.font.pointSize() - 2))

        content = rect.adjusted(8, 5, -8, -5)
        running_space = 16 if is_running else 0
        title_rect = QRect(content.left(), content.top(), max(20, content.width() - running_space), 17)
        meta_left = content.left()
        if has_work_dir:
            if selected:
                pixmap = Icons.get_colored(Icons.FOLDER, meta_color.name(), scale_factor=0.7).pixmap(13, 13)
            else:
                pixmap = Icons.get_colored(Icons.FOLDER, meta_color.name(), scale_factor=0.7).pixmap(13, 13)
            icon_y = content.top() + 23
            painter.drawPixmap(content.left(), icon_y, pixmap)
            meta_left += 17
        meta_rect = QRect(meta_left, content.top() + 21, max(20, content.right() - meta_left), 15)

        title_metrics = QFontMetrics(title_font)
        meta_metrics = QFontMetrics(meta_font)
        painter.setPen(title_color)
        painter.setFont(title_font)
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title_metrics.elidedText(title, Qt.TextElideMode.ElideRight, title_rect.width()))
        painter.setPen(meta_color)
        painter.setFont(meta_font)
        painter.drawText(meta_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, meta_metrics.elidedText(meta, Qt.TextElideMode.ElideRight, meta_rect.width()))
        if is_running:
            accent = QColor(colors["primary"])
            center_x = content.right() - 5
            center_y = content.top() + 8
            painter.setPen(QPen(accent, 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QRect(center_x - 4, center_y - 4, 8, 8))
        painter.restore()


class Sidebar(QWidget):
    """Sidebar with conversation list"""
    
    conversation_selected = pyqtSignal(str)
    new_conversation = pyqtSignal()
    import_conversation = pyqtSignal(str)
    delete_conversation = pyqtSignal(str)
    export_conversation = pyqtSignal(str, str)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumWidth(160)
        self.setMaximumWidth(260)
        self._all_conversations = []
        self._streaming_conversation_ids: set[str] = set()
        self._setup_ui()
        
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # Header
        header = QWidget()
        header.setObjectName("sidebar_header")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(10, 10, 10, 6)
        header_layout.setSpacing(6)
        
        # title = QLabel("PyCat")
        # title.setObjectName("sidebar_title")
        # header_layout.addWidget(title)
        
        self.new_chat_btn = QPushButton("+ 新建会话")
        self.new_chat_btn.setObjectName("new_chat_btn")
        self.new_chat_btn.clicked.connect(self.new_conversation.emit)
        header_layout.addWidget(self.new_chat_btn)
        
        self.search_input = ThemedLineEdit()
        self.search_input.setObjectName("search_input")
        self.search_input.setPlaceholderText("搜索标题或消息...")
        self.search_input.textChanged.connect(self._filter_conversations)
        header_layout.addWidget(self.search_input)
        
        layout.addWidget(header)
        
        # Conversation list
        self.conversation_list = QListWidget()
        self.conversation_list.setObjectName("conversation_list")
        self.conversation_list.setItemDelegate(ConversationItemDelegate(self.conversation_list))
        self.conversation_list.setUniformItemSizes(True)
        self.conversation_list.setMouseTracking(True)
        self.conversation_list.itemClicked.connect(self._on_item_clicked)
        self.conversation_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.conversation_list.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self.conversation_list)
        
        # Footer
        footer = QWidget()
        footer.setObjectName("sidebar_footer")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(10, 6, 10, 10)
        
        import_btn = QPushButton("导入 JSON")
        import_btn.setObjectName("import_btn")
        import_btn.clicked.connect(self.prompt_import_conversation)
        footer_layout.addWidget(import_btn)
        
        layout.addWidget(footer)
    
    def update_conversations(self, conversations: List[Dict[str, Any]]):
        self.conversation_list.setUpdatesEnabled(False)
        self.conversation_list.clear()
        self._all_conversations = conversations
        
        for conv in conversations:
            item = ConversationItem(conv)
            item.set_streaming_state(
                str(conv.get("id", "") or "") in self._streaming_conversation_ids
            )
            self.conversation_list.addItem(item)
        self._filter_conversations(self.search_input.text())
        self.conversation_list.setUpdatesEnabled(True)

    def set_conversation_streaming(self, conversation_id: str, streaming: bool) -> None:
        target = str(conversation_id or "").strip()
        if not target:
            return
        if streaming:
            self._streaming_conversation_ids.add(target)
        else:
            self._streaming_conversation_ids.discard(target)
        for row in range(self.conversation_list.count()):
            item = self.conversation_list.item(row)
            if isinstance(item, ConversationItem) and str(item.conversation_data.get("id", "") or "") == target:
                item.set_streaming_state(bool(streaming))
                self.conversation_list.viewport().update(self.conversation_list.visualItemRect(item))
                break

    def set_streaming_conversations(self, conversation_ids) -> None:
        self._streaming_conversation_ids = {
            str(item or "").strip() for item in (conversation_ids or ()) if str(item or "").strip()
        }
        for row in range(self.conversation_list.count()):
            item = self.conversation_list.item(row)
            if isinstance(item, ConversationItem):
                item.set_streaming_state(
                    str(item.conversation_data.get("id", "") or "") in self._streaming_conversation_ids
                )
        self.conversation_list.viewport().update()
    
    def select_conversation(self, conversation_id: str):
        for i in range(self.conversation_list.count()):
            item = self.conversation_list.item(i)
            if isinstance(item, ConversationItem) and item.conversation_data.get('id') == conversation_id:
                self.conversation_list.setCurrentItem(item)
                break
    
    def _filter_conversations(self, text: str):
        for i in range(self.conversation_list.count()):
            item = self.conversation_list.item(i)
            if isinstance(item, ConversationItem):
                item.setHidden(not item.matches_search(text))
        self.conversation_list.viewport().update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
    
    def _on_item_clicked(self, item: QListWidgetItem):
        if isinstance(item, ConversationItem):
            self.conversation_selected.emit(item.conversation_data.get('id', ''))
    
    def _show_context_menu(self, position):
        item = self.conversation_list.itemAt(position)
        if not isinstance(item, ConversationItem):
            return
        
        menu = prepare_context_menu(QMenu(self), self)
        conversation_id = str(item.conversation_data.get('id', '') or '')

        copy_id_action = QAction("复制 Session ID", self)
        copy_id_action.triggered.connect(lambda: self._copy_session_id(conversation_id))
        menu.addAction(copy_id_action)

        export_menu = prepare_context_menu(QMenu("导出会话", self), self)
        export_md_action = QAction("导出为 Markdown...", self)
        export_md_action.triggered.connect(lambda: self.export_conversation.emit(conversation_id, "markdown"))
        export_menu.addAction(export_md_action)
        export_json_action = QAction("导出为 JSON...", self)
        export_json_action.triggered.connect(lambda: self.export_conversation.emit(conversation_id, "json"))
        export_menu.addAction(export_json_action)
        menu.addMenu(export_menu)
        menu.addSeparator()

        delete_action = QAction("删除", self)
        delete_action.triggered.connect(
            lambda: self._confirm_delete(conversation_id)
        )
        menu.addAction(delete_action)
        menu.exec(self.conversation_list.mapToGlobal(position))

    def _copy_session_id(self, conversation_id: str):
        text = str(conversation_id or "").strip()
        if not text:
            return
        QApplication.clipboard().setText(text)
    
    def _confirm_delete(self, conversation_id: str):
        reply = QMessageBox.question(
            self, '删除会话',
            '确定要删除这个会话吗？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.delete_conversation.emit(conversation_id)
    
    def prompt_import_conversation(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, '导入会话', '',
            'JSON 文件 (*.json);;所有文件 (*)'
        )
        if file_path:
            self.import_conversation.emit(file_path)
