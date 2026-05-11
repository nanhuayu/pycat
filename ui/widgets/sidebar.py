"""
Sidebar widget for conversation list and management - Chinese UI
"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, 
    QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QMessageBox, QFileDialog, QLabel, QApplication
)
from PyQt6.QtCore import pyqtSignal, Qt, QTimer, QSize
from PyQt6.QtGui import QAction, QFontMetrics
from typing import Dict, Any, List
from datetime import datetime


class ConversationItem(QListWidgetItem):
    """Custom list item for conversations - compact display"""
    
    def __init__(self, data: Dict[str, Any]):
        super().__init__()
        self.data = data
        self.title = str(data.get('title', '无标题') or '无标题')
        self.model = str(data.get('model', '') or '')
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
        self.setToolTip(
            f"标题: {self.title}\nSession ID: {session_id or '-'}\n"
            f"模型: {self.model or '未设置'}\n消息: {count} 条\n更新: {self.updated_str or '-'}"
        )
        self.setSizeHint(QSize(0, 34))

    def refresh_text(self, metrics: QFontMetrics, available_width: int) -> None:
        width = max(72, int(available_width or 0))
        elided_title = metrics.elidedText(self.title, Qt.TextElideMode.ElideRight, width)
        self.setText(elided_title)


class Sidebar(QWidget):
    """Sidebar with conversation list"""
    
    conversation_selected = pyqtSignal(str)
    new_conversation = pyqtSignal()
    import_conversation = pyqtSignal(str)
    delete_conversation = pyqtSignal(str)
    duplicate_conversation = pyqtSignal(str)
    export_conversation = pyqtSignal(str, str)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumWidth(160)
        self.setMaximumWidth(260)
        self._all_conversations = []
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
        
        self.search_input = QLineEdit()
        self.search_input.setObjectName("search_input")
        self.search_input.setPlaceholderText("搜索会话...")
        self.search_input.textChanged.connect(self._filter_conversations)
        header_layout.addWidget(self.search_input)
        
        layout.addWidget(header)
        
        # Conversation list
        self.conversation_list = QListWidget()
        self.conversation_list.setObjectName("conversation_list")
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
        self.conversation_list.clear()
        self._all_conversations = conversations
        
        for conv in conversations:
            item = ConversationItem(conv)
            self.conversation_list.addItem(item)
        self._refresh_item_texts()
        QTimer.singleShot(0, self._refresh_item_texts)
    
    def select_conversation(self, conversation_id: str):
        for i in range(self.conversation_list.count()):
            item = self.conversation_list.item(i)
            if isinstance(item, ConversationItem) and item.data.get('id') == conversation_id:
                self.conversation_list.setCurrentItem(item)
                break
    
    def _filter_conversations(self, text: str):
        text = text.lower()
        for i in range(self.conversation_list.count()):
            item = self.conversation_list.item(i)
            if isinstance(item, ConversationItem):
                title = item.data.get('title', '').lower()
                item.setHidden(text not in title)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh_item_texts()

    def _refresh_item_texts(self):
        metrics = self.conversation_list.fontMetrics()
        available_width = max(96, self.conversation_list.viewport().width() - 18)
        for i in range(self.conversation_list.count()):
            item = self.conversation_list.item(i)
            if isinstance(item, ConversationItem):
                item.refresh_text(metrics, available_width)
    
    def _on_item_clicked(self, item: QListWidgetItem):
        if isinstance(item, ConversationItem):
            self.conversation_selected.emit(item.data.get('id', ''))
    
    def _show_context_menu(self, position):
        item = self.conversation_list.itemAt(position)
        if not isinstance(item, ConversationItem):
            return
        
        menu = QMenu(self)
        conversation_id = str(item.data.get('id', '') or '')

        copy_id_action = QAction("复制 Session ID", self)
        copy_id_action.triggered.connect(lambda: self._copy_session_id(conversation_id))
        menu.addAction(copy_id_action)

        export_menu = QMenu("导出会话", self)
        export_md_action = QAction("导出为 Markdown...", self)
        export_md_action.triggered.connect(lambda: self.export_conversation.emit(conversation_id, "markdown"))
        export_menu.addAction(export_md_action)
        export_json_action = QAction("导出为 JSON...", self)
        export_json_action.triggered.connect(lambda: self.export_conversation.emit(conversation_id, "json"))
        export_menu.addAction(export_json_action)
        menu.addMenu(export_menu)
        menu.addSeparator()

        duplicate_action = QAction("复制会话", self)
        duplicate_action.triggered.connect(
            lambda: self.duplicate_conversation.emit(conversation_id)
        )
        menu.addAction(duplicate_action)
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
