"""
Chat view widget - Compact responsive layout
"""

import logging
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QLabel, QFrame, QSizePolicy, QPushButton, QToolButton, QFileDialog,
    QStackedWidget,
)
from PyQt6.QtCore import pyqtSignal, Qt, QTimer, QEvent, QSize
from typing import Callable, List
import os

from core.llm.token_budget import TokenUsageSnapshot
from core.llm.token_budget import format_token_count
from models.conversation import Message, Conversation
from models.provider import Provider
from .message_widget import InlineQuestionCard, MessageWidget, MarkdownView
from .chat.streaming_overlay import StreamingOverlay
from ui.utils.image_utils import extract_images_from_mime, extract_images_from_clipboard
from ui.utils.icon_manager import Icons


logger = logging.getLogger(__name__)


class ChatView(QWidget):
    """Scrollable view for displaying chat messages"""
    
    edit_message = pyqtSignal(str)
    delete_message = pyqtSignal(str)
    images_dropped = pyqtSignal(list)
    work_dir_changed = pyqtSignal(str)  # Signal emitted when workspace directory changes
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("chat_container")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._message_widgets: List[MessageWidget] = []
        self._inline_question_card: InlineQuestionCard | None = None
        self._inline_question_cancel_callback: Callable[[], None] | None = None
        self._nav_update_timer: QTimer | None = None
        self._stream = StreamingOverlay(scroll_area=None, should_auto_scroll=self._should_follow_output)  # scroll_area set after _setup_ui
        self._follow_output = True
        
        self._setup_ui()
        self._stream._scroll_area = self.scroll_area
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # ===== Header bar with model indicator =====
        self.header_bar = QFrame()
        self.header_bar.setObjectName("chat_header")
        self.header_bar.setFixedHeight(42)
        
        header_layout = QHBoxLayout(self.header_bar)
        header_layout.setContentsMargins(6, 5, 6, 5)
        header_layout.setSpacing(6)
        
        # ===== Workspace/Folder Button =====
        self.work_dir_btn = QPushButton()
        self.work_dir_btn.setIcon(Icons.get(Icons.FOLDER))
        self.work_dir_btn.setText(" 未设置工作区")
        self.work_dir_btn.setObjectName("work_dir_btn")
        self.work_dir_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.work_dir_btn.setToolTip("点击设置当前会话的工作目录 (用于 MCP/CMD 执行)")
        self.work_dir_btn.setIconSize(QSize(Icons.SIZE_NAV, Icons.SIZE_NAV))
        self.work_dir_btn.setFixedHeight(30)
        self.work_dir_btn.setMaximumWidth(240)
        self.work_dir_btn.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.work_dir_btn.clicked.connect(self._select_work_dir)
        header_layout.addWidget(self.work_dir_btn)

        # Separator
        sep = QFrame()
        sep.setObjectName("chat_header_separator")
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        sep.setFixedHeight(16)
        header_layout.addWidget(sep)

        self.message_count_label = QLabel("0 条")
        self.message_count_label.setObjectName("context_indicator")
        self.message_count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message_count_label.setFixedHeight(30)
        self.message_count_label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        header_layout.addWidget(self.message_count_label)

        self.runtime_indicator = QLabel("空闲")
        self.runtime_indicator.setObjectName("runtime_indicator")
        self.runtime_indicator.setProperty("active", False)
        self.runtime_indicator.setToolTip("等待下一次请求")
        self.runtime_indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.runtime_indicator.setFixedHeight(30)
        self.runtime_indicator.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        header_layout.addWidget(self.runtime_indicator)
        
        header_layout.addStretch()

        # ===== Message navigation (toolbar-style group) =====
        header_layout.addWidget(self._create_nav_bar())

        layout.addWidget(self.header_bar)

        self.body_stack = QStackedWidget()
        self.body_stack.setObjectName("chat_body_stack")
        
        # ===== Messages scroll area =====
        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("messages_scroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        
        self.messages_container = QWidget()
        self.messages_container.setObjectName("messages_container")
        self.messages_layout = QVBoxLayout(self.messages_container)
        # Align list padding with header bar margins for a cleaner vertical rhythm.
        self.messages_layout.setContentsMargins(10, 10, 10, 10)
        self.messages_layout.setSpacing(6)
        self.messages_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._bottom_spacer = QWidget()
        self._bottom_spacer.setFixedHeight(8)
        self._bottom_spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.messages_layout.addWidget(self._bottom_spacer)
        
        self.scroll_area.setWidget(self.messages_container)

        self.empty_state_page = self._create_empty_state_page()
        self.body_stack.addWidget(self.empty_state_page)
        self.body_stack.addWidget(self.scroll_area)
        layout.addWidget(self.body_stack)

        # Allow dropping images anywhere in the chat area (message list viewport)
        try:
            self.scroll_area.setAcceptDrops(True)
            self.scroll_area.viewport().setAcceptDrops(True)
            self.scroll_area.viewport().installEventFilter(self)
            self.scroll_area.viewport().setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self.empty_state_page.setAcceptDrops(True)
            self.empty_state_page.installEventFilter(self)
            self.empty_state_page.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        except Exception as exc:
            logger.debug("Failed to enable drag-and-drop on chat view: %s", exc)

        # Debounced nav state updates (scrolling can emit valueChanged frequently)
        self._nav_update_timer = QTimer(self)
        self._nav_update_timer.setSingleShot(True)
        self._nav_update_timer.timeout.connect(self._update_nav_state)
        self.scroll_area.verticalScrollBar().valueChanged.connect(self._schedule_nav_update)
        self.scroll_area.verticalScrollBar().valueChanged.connect(self._update_follow_output)

        self._update_nav_state()
        self._update_empty_state()

    def eventFilter(self, watched, event):
        # Handle drag/drop on the scroll area viewport (the actual visible chat area).
        try:
            if watched in {self.scroll_area.viewport(), self.empty_state_page}:
                if event.type() == QEvent.Type.KeyPress:
                    # Allow pasting screenshot images when focus is on the chat area.
                    try:
                        key = event.key()
                        mods = event.modifiers()
                        if key == Qt.Key.Key_V and (mods & Qt.KeyboardModifier.ControlModifier):
                            sources = extract_images_from_clipboard()
                            if sources:
                                self.images_dropped.emit(sources)
                                return True
                    except Exception as exc:
                        logger.debug("Failed to handle chat view clipboard paste: %s", exc)

                if event.type() == QEvent.Type.DragEnter:
                    md = event.mimeData()
                    data_urls, file_paths = extract_images_from_mime(md)
                    if data_urls or file_paths:
                        event.acceptProposedAction()
                        return True
                elif event.type() == QEvent.Type.Drop:
                    md = event.mimeData()
                    data_urls, file_paths = extract_images_from_mime(md)
                    sources = data_urls + file_paths
                    if sources:
                        event.acceptProposedAction()
                        self.images_dropped.emit(sources)
                        return True
        except Exception as exc:
            logger.debug("Failed during chat view drag/drop event handling: %s", exc)

        return super().eventFilter(watched, event)

    def _create_nav_bar(self) -> QWidget:
        nav_group = QFrame()
        nav_group.setObjectName("chat_nav_group")
        nav_layout = QHBoxLayout(nav_group)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(2)

        self.nav_top_btn = self._create_nav_button(Icons.ANGLES_UP, "滚动到顶部")
        self.nav_top_btn.clicked.connect(self._scroll_to_top)
        nav_layout.addWidget(self.nav_top_btn)

        self.nav_prev_btn = self._create_nav_button(Icons.CHEVRON_UP, "上一条消息")
        self.nav_prev_btn.clicked.connect(self.go_prev_message)
        nav_layout.addWidget(self.nav_prev_btn)

        self.nav_next_btn = self._create_nav_button(Icons.CHEVRON_DOWN, "下一条消息")
        self.nav_next_btn.clicked.connect(self.go_next_message)
        nav_layout.addWidget(self.nav_next_btn)

        self.nav_bottom_btn = self._create_nav_button(Icons.ANGLES_DOWN, "滚动到底部")
        self.nav_bottom_btn.clicked.connect(self._scroll_to_bottom)
        nav_layout.addWidget(self.nav_bottom_btn)
        
        return nav_group

    def _create_nav_button(self, icon_name: str, tooltip: str) -> QToolButton:
        btn = QToolButton()
        btn.setIcon(Icons.get_muted(icon_name))
        btn.setIconSize(QSize(14, 14))
        btn.setToolTip(tooltip)
        btn.setObjectName("toolbar_btn")
        btn.setFixedSize(30, 30)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return btn

    def _create_empty_state_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("chat_empty_state")

        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 16, 28, 16)
        layout.setSpacing(0)
        layout.addStretch(1)

        card = QFrame()
        card.setObjectName("chat_empty_card")
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        card.setMaximumWidth(560)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(22, 18, 22, 18)
        card_layout.setSpacing(12)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)
        title_row.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        hero_icon = QLabel()
        hero_icon.setObjectName("chat_empty_hero_icon")
        hero_icon.setFixedSize(32, 32)
        hero_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero_icon.setPixmap(Icons.get(Icons.PYCAT).pixmap(Icons.SIZE_EMPTY_HERO, Icons.SIZE_EMPTY_HERO))
        title_row.addWidget(hero_icon)

        title = QLabel("开始新对话")
        title.setObjectName("chat_empty_title")
        title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        title_row.addWidget(title)
        card_layout.addLayout(title_row)

        description = QLabel(
            "选好工作区和模型，然后直接写下目标。截图可以拖入，也可以 Ctrl+V 粘贴。"
        )
        description.setObjectName("chat_empty_description")
        description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        description.setWordWrap(True)
        card_layout.addWidget(description)

        tips_row = QHBoxLayout()
        tips_row.setSpacing(8)
        tips_row.addWidget(
            self._create_empty_tip_card(
                Icons.PAGE_MODELS,
                "工作区与模型",
                "保持上下文明确，后续执行更稳。",
            )
        )
        tips_row.addWidget(
            self._create_empty_tip_card(
                Icons.PAPERCLIP,
                "图片与文件",
                "拖入、粘贴或引用文件都可以。",
            )
        )
        card_layout.addLayout(tips_row)

        layout.addWidget(card, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)
        return page

    def _create_empty_tip_card(self, icon_name: str, title: str, description: str) -> QFrame:
        card = QFrame()
        card.setObjectName("chat_empty_tip_card")
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 9, 12, 10)
        layout.setSpacing(6)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(6)

        icon_label = QLabel()
        icon_label.setObjectName("chat_empty_tip_icon")
        icon_label.setFixedSize(20, 20)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setPixmap(Icons.get(icon_name, color=Icons.COLOR_MUTED, scale_factor=1.0).pixmap(18, 18))
        title_row.addWidget(icon_label)

        title_label = QLabel(title)
        title_label.setObjectName("chat_empty_tip_title")
        title_label.setWordWrap(True)
        title_row.addWidget(title_label, 1)
        layout.addLayout(title_row)

        desc_label = QLabel(description)
        desc_label.setObjectName("chat_empty_tip_description")
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)

        return card

    def _update_empty_state(self) -> None:
        if not hasattr(self, "body_stack"):
            return
        has_content = bool(self._message_widgets or self._inline_question_card or self._stream.active)
        self.body_stack.setCurrentWidget(self.scroll_area if has_content else self.empty_state_page)
    
    def _select_work_dir(self):
        """Open dialog to select workspace directory"""
        current_dir = ""
        # Try to get current path from button tooltip or text if possible, 
        # but better to rely on state passed from controller. 
        # For now, start from current working directory or last used.
        
        path = QFileDialog.getExistingDirectory(
            self, 
            "选择工作区文件夹",
            current_dir,
            QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontResolveSymlinks
        )
        
        if path:
            self.update_work_dir(path)
            self.work_dir_changed.emit(path)

    def update_work_dir(self, path: str):
        """Update workspace directory display"""
        if not path:
            self.work_dir_btn.setIcon(Icons.get(Icons.FOLDER))
            self.work_dir_btn.setText(" 未设置工作区")
            self.work_dir_btn.setToolTip("点击设置当前会话的工作目录")
        else:
            name = os.path.basename(path)
            if not name: # Root directory like C:/
                name = path
            self.work_dir_btn.setIcon(Icons.get_colored(Icons.FOLDER, Icons.COLOR_SUCCESS))
            self.work_dir_btn.setText(f" {name}")
            self.work_dir_btn.setToolTip(f"工作区: {path}")

    def update_header(self, model_ref: str, msg_count: int = 0, token_snapshot: TokenUsageSnapshot | None = None):
        """Update header info"""
        text = model_ref or "未选择模型"
        if token_snapshot is not None:
            used = format_token_count(token_snapshot.context_tokens)
            limit = format_token_count(token_snapshot.effective_prompt_limit or token_snapshot.context_window)
            self.message_count_label.setText(f"{used}/{limit}")
            self.message_count_label.setToolTip(self._format_token_tooltip(token_snapshot))
        else:
            self.message_count_label.setText(f"{int(msg_count or 0)} 条")
            self.message_count_label.setToolTip(f"当前会话消息数：{int(msg_count or 0)}")

    def _format_token_tooltip(self, snapshot: TokenUsageSnapshot) -> str:
        lines = [
            f"窗口: {format_token_count(snapshot.context_window)}",
            f"已用: {format_token_count(snapshot.context_tokens)}",
            f"预留输出: {format_token_count(snapshot.reserved_output_tokens)}",
            f"剩余: {format_token_count(snapshot.remaining_prompt_tokens)}",
            f"占用: {snapshot.usage_ratio * 100:.1f}%",
        ]
        if snapshot.budget.model_id:
            lines.append(f"模型: {snapshot.budget.model_id}")
        if snapshot.budget.provider_name:
            lines.append(f"提供方: {snapshot.budget.provider_name}")
        if snapshot.status != "ok":
            lines.append(f"状态: {snapshot.status}")
        return "\n".join(lines)

    def set_model_options(self, providers: list[Provider], current_model_ref: str = "") -> None:
        return

    def update_runtime_state(self, stream_state=None) -> None:
        title, detail, active = self._resolve_runtime_labels(stream_state)
        self.runtime_indicator.setText(title)
        self.runtime_indicator.setToolTip(detail or title)
        self.runtime_indicator.setProperty("active", bool(active))
        self.runtime_indicator.style().unpolish(self.runtime_indicator)
        self.runtime_indicator.style().polish(self.runtime_indicator)
    
    def clear(self):
        self.clear_inline_question(notify=True)
        while self.messages_layout.count() > 1:
            item = self.messages_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._message_widgets.clear()
        self._stream.finish()
        self.update_runtime_state(None)
        self._update_nav_state()
        self._update_empty_state()
    
    def load_conversation(self, conversation: Conversation):
        self.clear()
        for message in conversation.messages:
            self.add_message(message)
        QTimer.singleShot(100, self._scroll_to_bottom)
        self._schedule_nav_update()
    
    def add_message(self, message: Message):
        # Create widget
        widget = MessageWidget(message)
        widget.edit_requested.connect(self.edit_message.emit)
        widget.delete_requested.connect(self.delete_message.emit)
        
        self._insert_above_bottom_spacer(widget)
        self._message_widgets.append(widget)
        self._update_empty_state()
        if self._should_follow_output():
            QTimer.singleShot(50, self._scroll_to_bottom)
        self._schedule_nav_update()

    def update_subtask_trace(self, trace: dict) -> bool:
        if not isinstance(trace, dict):
            return False
        metadata = trace.get('metadata') if isinstance(trace.get('metadata'), dict) else {}
        parent_message_id = str(trace.get('parent_message_id') or metadata.get('parent_message_id') or '').strip()
        tool_call_id = str(
            trace.get('parent_tool_call_id')
            or trace.get('tool_call_id')
            or metadata.get('parent_tool_call_id')
            or metadata.get('tool_call_id')
            or ''
        ).strip()
        trace_id = str(trace.get('id') or '').strip()
        if parent_message_id:
            for i in range(len(self._message_widgets) - 1, -1, -1):
                widget = self._message_widgets[i]
                if str(getattr(widget.message, 'id', '') or '') != parent_message_id:
                    continue
                if tool_call_id and not widget.has_tool_call(tool_call_id):
                    break
                if widget.update_subtask_trace(trace):
                    if i == len(self._message_widgets) - 1 and self._should_follow_output():
                        QTimer.singleShot(50, self._scroll_to_bottom)
                    self._schedule_nav_update()
                    return True
        for i in range(len(self._message_widgets) - 1, -1, -1):
            widget = self._message_widgets[i]
            if tool_call_id and not widget.has_tool_call(tool_call_id):
                continue
            if widget.update_subtask_trace(trace):
                if i == len(self._message_widgets) - 1 and self._should_follow_output():
                    QTimer.singleShot(50, self._scroll_to_bottom)
                self._schedule_nav_update()
                return True
        logger.debug("No parent assistant widget found for subtask trace %s / tool %s", trace_id, tool_call_id)
        return False

    def show_inline_question(
        self,
        question: dict,
        *,
        on_submit: Callable[[dict], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
    ) -> None:
        self.clear_inline_question(notify=False)

        card = InlineQuestionCard(question)

        def _handle_submit(answer: object) -> None:
            try:
                if on_submit is not None:
                    on_submit(dict(answer or {}))
            finally:
                self.clear_inline_question(notify=False)

        def _handle_cancel() -> None:
            try:
                if on_cancel is not None:
                    on_cancel()
            finally:
                self.clear_inline_question(notify=False)

        card.submitted.connect(_handle_submit)
        card.cancelled.connect(_handle_cancel)

        self._inline_question_card = card
        self._inline_question_cancel_callback = on_cancel
        self._insert_above_bottom_spacer(card)
        self._update_empty_state()
        if self._should_follow_output():
            QTimer.singleShot(50, self._scroll_to_bottom)
        self._schedule_nav_update()

    def clear_inline_question(self, *, notify: bool = False) -> None:
        card = self._inline_question_card
        cancel_callback = self._inline_question_cancel_callback
        self._inline_question_card = None
        self._inline_question_cancel_callback = None

        if card is not None:
            try:
                self.messages_layout.removeWidget(card)
            except Exception:
                pass
            card.deleteLater()

        if notify and cancel_callback is not None:
            try:
                cancel_callback()
            except Exception as exc:
                logger.debug("Failed to notify inline question cancellation: %s", exc)
        self._update_empty_state()
    
    def start_streaming_response(self, model: str = ""):
        self._follow_output = self._is_near_bottom()
        self._stream.start(model=model, parent_layout=self.messages_layout, insert_index=self._bottom_insert_index())
        self._update_empty_state()
    
    def append_streaming_content(self, content: str):
        self._stream.append_content(content)

    def append_streaming_thinking(self, thinking: str):
        self._stream.append_thinking(thinking)

    def restore_streaming_state(self, visible_text: str = "", thinking_text: str = "") -> None:
        """Restore streaming UI from cached buffers (used when switching back to a streaming conversation)."""
        self._stream.restore(visible_text, thinking_text)

    def finish_streaming_response(self, message: Message, add_to_view: bool = True):
        self._stream.finish()
        
        # Only add message to view if requested (to avoid duplicates)
        if add_to_view:
            self.add_message(message)
        self._update_empty_state()
        self._schedule_nav_update()
    
    def is_streaming(self) -> bool:
        """Check if currently in streaming mode."""
        return self._stream.active

    def update_message(self, message: Message):
        for i, widget in enumerate(self._message_widgets):
            if widget.message.id == message.id:
                index = self.messages_layout.indexOf(widget)
                widget.deleteLater()
                
                new_widget = MessageWidget(message)
                new_widget.edit_requested.connect(self.edit_message.emit)
                new_widget.delete_requested.connect(self.delete_message.emit)
                
                self.messages_layout.insertWidget(index, new_widget)
                self._message_widgets[i] = new_widget
                break
        self._schedule_nav_update()
        self._update_empty_state()

    def refresh_message_tool_calls(
        self,
        message_id: str = "",
        tool_call_id: str = "",
        *,
        updated_message: Message | None = None,
    ) -> bool:
        """Refresh an existing assistant widget after a tool result is attached."""
        target_message_id = str(message_id or "").strip()
        target_tool_call_id = str(tool_call_id or "").strip()
        for i, widget in enumerate(self._message_widgets):
            if target_message_id and str(getattr(widget.message, "id", "") or "") != target_message_id:
                continue
            if target_tool_call_id and not widget.has_tool_call(target_tool_call_id):
                continue
            if updated_message is not None:
                self.update_message(updated_message)
            else:
                widget.refresh_tool_calls()
            if i == len(self._message_widgets) - 1 and self._should_follow_output():
                QTimer.singleShot(50, self._scroll_to_bottom)
            self._schedule_nav_update()
            return True
        return False
    
    def remove_message(self, message_id: str):
        for widget in self._message_widgets[:]:
            if widget.message.id == message_id:
                widget.deleteLater()
                self._message_widgets.remove(widget)
                break
        self._schedule_nav_update()
        self._update_empty_state()

    def _schedule_nav_update(self):
        if not self._nav_update_timer:
            return
        self._nav_update_timer.start(30)

    def _update_follow_output(self, _value: int | None = None) -> None:
        self._follow_output = self._is_near_bottom()

    def _is_near_bottom(self, threshold: int = 96) -> bool:
        try:
            scrollbar = self.scroll_area.verticalScrollBar()
            return (scrollbar.maximum() - scrollbar.value()) <= int(threshold)
        except Exception:
            return True

    def _should_follow_output(self) -> bool:
        if not self._stream.active:
            return self._is_near_bottom()
        return bool(self._follow_output or self._is_near_bottom())

    def _bottom_insert_index(self) -> int:
        try:
            index = self.messages_layout.indexOf(self._bottom_spacer)
            if index >= 0:
                return index
        except Exception:
            pass
        return max(0, self.messages_layout.count())

    def _insert_above_bottom_spacer(self, widget: QWidget) -> None:
        self.messages_layout.insertWidget(self._bottom_insert_index(), widget, 0, Qt.AlignmentFlag.AlignTop)

    def _navigable_widgets(self) -> List[MessageWidget]:
        return [w for w in self._message_widgets if w is not None]

    def _find_current_message_index(self) -> int:
        widgets = self._navigable_widgets()
        if not widgets:
            return -1

        scrollbar = self.scroll_area.verticalScrollBar()
        top = int(scrollbar.value()) + 2

        for i, w in enumerate(widgets):
            try:
                if (w.y() + w.height()) >= top:
                    return i
            except Exception:
                continue
        return len(widgets) - 1

    def _scroll_to_message_index(self, index: int):
        widgets = self._navigable_widgets()
        if not widgets:
            return

        index = max(0, min(int(index), len(widgets) - 1))
        w = widgets[index]
        try:
            y = max(int(w.y()) - 4, 0)
        except Exception:
            return

        scrollbar = self.scroll_area.verticalScrollBar()
        scrollbar.setValue(y)
        self._schedule_nav_update()

    def go_prev_message(self):
        idx = self._find_current_message_index()
        if idx <= 0:
            return
        self._scroll_to_message_index(idx - 1)

    def go_next_message(self):
        widgets = self._navigable_widgets()
        if not widgets:
            return
        idx = self._find_current_message_index()
        if idx < 0:
            self._scroll_to_message_index(0)
            return
        if idx >= (len(widgets) - 1):
            return
        self._scroll_to_message_index(idx + 1)

    def _update_nav_state(self):
        widgets = self._navigable_widgets()
        total = len(widgets)
        if total <= 0:
            self.nav_top_btn.setEnabled(False)
            self.nav_prev_btn.setEnabled(False)
            self.nav_next_btn.setEnabled(False)
            self.nav_bottom_btn.setEnabled(False)
            
            self.nav_top_btn.setToolTip("滚动到顶部")
            self.nav_prev_btn.setToolTip("上一条消息")
            self.nav_next_btn.setToolTip("下一条消息")
            self.nav_bottom_btn.setToolTip("滚动到底部")
            return

        idx = self._find_current_message_index()
        if idx < 0:
            idx = 0

        self.nav_top_btn.setEnabled(True)
        self.nav_prev_btn.setEnabled(idx > 0)
        self.nav_next_btn.setEnabled(idx < total - 1)
        self.nav_bottom_btn.setEnabled(True)

        # Keep the UI minimal: show position in tooltips instead of an always-visible counter.
        pos_text = f"{idx + 1}/{total}"
        self.nav_top_btn.setToolTip(f"滚动到顶部 (共 {total} 条)")
        self.nav_prev_btn.setToolTip(f"上一条消息 ({pos_text})")
        self.nav_next_btn.setToolTip(f"下一条消息 ({pos_text})")
        self.nav_bottom_btn.setToolTip(f"滚动到底部 (共 {total} 条)")
    
    def _scroll_to_bottom(self):
        scrollbar = self.scroll_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        self._follow_output = True

    def _scroll_to_top(self):
        scrollbar = self.scroll_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.minimum())
        self._follow_output = False

    @staticmethod
    def _runtime_event_label(kind: str) -> str:
        labels = {
            "turn_start": "开始执行",
            "tool_start": "工具中",
            "tool_end": "工具完成",
            "retry": "重试中",
            "complete": "已完成",
            "error": "出错",
            "step": "处理中",
        }
        return labels.get(str(kind or ""), str(kind or "运行中"))

    def _resolve_runtime_labels(self, stream_state) -> tuple[str, str, bool]:
        if stream_state is None:
            return ("空闲", "等待下一次请求", False)

        active_tool = str(getattr(stream_state, "active_tool", "") or "").strip()
        last_kind = str(getattr(stream_state, "last_event_kind", "") or "").strip()
        last_detail = str(getattr(stream_state, "last_event_detail", "") or "").strip()
        if active_tool:
            return (f"工具 · {active_tool}", last_detail or "正在等待工具返回", True)
        if last_kind:
            return (self._runtime_event_label(last_kind), last_detail or "-", True)

        model = str(getattr(stream_state, "model", "") or "").strip()
        return ("生成中", model or "正在等待模型响应", True)

