"""
Chat view widget - Compact responsive layout
"""

import logging
from PyQt6.QtGui import QPainter, QPalette
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QLabel, QFrame, QSizePolicy, QPushButton, QToolButton, QFileDialog,
    QStackedWidget, QStyleOption,
)
from PyQt6.QtCore import pyqtSignal, Qt, QTimer, QEvent, QSize
from typing import Callable, List
import os

from models.conversation import Message, Conversation
from models.contracts.agent import RunStatus
from models.provider import Provider
from core.content.resolver import SessionContentResolver
from .inline_question_view import InlineQuestionCard
from .message_widget import MessageWidget
from .assistant_run_widget import AssistantRunWidget
from .markdown_view import MarkdownView
from .chat.streaming_overlay import StreamingOverlay
from core.content.references import delivery_refs_for_messages
from core.context.history import (
    is_real_user_message,
    is_restartable_user_message,
    restartable_user_by_id,
    restartable_user_for_assistant,
)
from core.channel.bindings import is_bound_channel_conversation
from gui.view_models.message_runs import (
    AssistantRunGroup,
    SingleMessageItem,
    project_message_runs,
)
from gui.utils.image_utils import (
    extract_attachment_sources_from_clipboard,
    extract_attachment_sources_from_mime,
)
from gui.utils.icon_manager import Icons
from gui.about_content import PRODUCT_NAME
from gui.widgets.themed_line_edit import ThemedSelectableLabel


logger = logging.getLogger(__name__)


# 0 disables tail-window trimming; the chat panel should show every message by default.
RECENT_MESSAGE_WINDOW = 0
EARLIER_MESSAGE_BATCH = 80


class _ElidingLabel(QLabel):
    """Single-line label that keeps its full text for tooltips and accessibility."""

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(0, super().minimumSizeHint().height())

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        del event
        option = QStyleOption()
        option.initFrom(self)
        painter = QPainter(self)
        text = self.fontMetrics().elidedText(
            self.text(),
            Qt.TextElideMode.ElideRight,
            max(0, self.contentsRect().width()),
        )
        self.style().drawItemText(
            painter,
            self.contentsRect(),
            self.alignment(),
            option.palette,
            self.isEnabled(),
            text,
            QPalette.ColorRole.WindowText,
        )


class ChatView(QWidget):
    """Scrollable view for displaying chat messages"""
    
    reuse_message = pyqtSignal(str)
    edit_message = pyqtSignal(str)
    regenerate_message = pyqtSignal(str)
    delete_message = pyqtSignal(str)
    continue_message = pyqtSignal(str)
    images_dropped = pyqtSignal(list)
    work_dir_changed = pyqtSignal(str)  # Signal emitted when workspace directory changes
    trace_requested = pyqtSignal()
    
    def __init__(self, parent=None, *, content_service=None):
        super().__init__(parent)
        self.setObjectName("chat_container")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._message_widgets: List[MessageWidget] = []
        self._render_widgets: List[QWidget] = []
        self._message_widget_by_id: dict[str, MessageWidget] = {}
        self._message_container_by_id: dict[str, QWidget] = {}
        self._conversation_messages: List[Message] = []
        self._conversation: Conversation | None = None
        self._content_service = content_service
        self._content_resolver = SessionContentResolver(content_service) if content_service is not None else None
        self._show_thinking = True
        self._work_dir = ""
        self._header_model_ref = ""
        self._header_message_count = 0
        self._runtime_detail = "等待下一次请求"
        self._render_start_index = 0
        self._history_window_expanded = False
        self._bulk_loading = False
        self._load_earlier_btn: QPushButton | None = None
        self._inline_question_card: InlineQuestionCard | None = None
        self._inline_question_cancel_callback: Callable[[], None] | None = None
        self._nav_update_timer: QTimer | None = None
        self._stream = StreamingOverlay(scroll_area=None, should_auto_scroll=self._should_follow_output)  # scroll_area set after _setup_ui
        self._follow_output = True
        self._revision_enabled = True
        
        self._setup_ui()
        self._header_notice_timer = QTimer(self)
        self._header_notice_timer.setSingleShot(True)
        self._header_notice_timer.timeout.connect(self.clear_header_notice)
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
        self.work_dir_btn.setIcon(Icons.get_muted(Icons.FOLDER))
        self.work_dir_btn.setText("")
        self.work_dir_btn.setObjectName("work_dir_btn")
        self.work_dir_btn.setAccessibleName("工作区")
        self.work_dir_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.work_dir_btn.setToolTip("未设置工作区；当前使用全局会话目录。点击设置")
        self.work_dir_btn.setIconSize(QSize(Icons.SIZE_NAV, Icons.SIZE_NAV))
        self.work_dir_btn.setFixedSize(30, 30)
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

        self.conversation_title_label = _ElidingLabel()
        self.conversation_title_label.setObjectName("conversation_title_label")
        self.conversation_title_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.conversation_title_label.setMinimumWidth(0)
        self.conversation_title_label.setMaximumWidth(320)
        self.conversation_title_label.setFixedHeight(30)
        self.conversation_title_label.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )
        self.conversation_title_label.setVisible(False)
        header_layout.addWidget(self.conversation_title_label)

        self.runtime_indicator = QToolButton()
        self.runtime_indicator.setObjectName("runtime_indicator")
        self.runtime_indicator.setText("空闲")
        self.runtime_indicator.setProperty("active", False)
        self.runtime_indicator.setToolTip("等待下一次请求")
        self.runtime_indicator.setAccessibleName("运行状态与调用链路")
        self.runtime_indicator.setCursor(Qt.CursorShape.PointingHandCursor)
        self.runtime_indicator.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.runtime_indicator.setFixedHeight(30)
        self.runtime_indicator.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.runtime_indicator.clicked.connect(self.trace_requested.emit)
        header_layout.addWidget(self.runtime_indicator)

        self.header_notice_slot = QFrame()
        self.header_notice_slot.setObjectName("header_notice_slot")
        self.header_notice_slot.setFixedHeight(30)
        self.header_notice_slot.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        notice_layout = QHBoxLayout(self.header_notice_slot)
        notice_layout.setContentsMargins(8, 0, 8, 0)
        notice_layout.setSpacing(0)
        self.header_notice_label = _ElidingLabel()
        self.header_notice_label.setObjectName("header_notice_label")
        self.header_notice_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.header_notice_label.setMinimumWidth(0)
        self.header_notice_label.setFixedHeight(30)
        self.header_notice_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.header_notice_label.setVisible(False)
        notice_layout.addWidget(self.header_notice_label)
        header_layout.addWidget(self.header_notice_slot, 1)

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
                            sources = extract_attachment_sources_from_clipboard()
                            if sources:
                                self.images_dropped.emit(sources)
                                return True
                    except Exception as exc:
                        logger.debug("Failed to handle chat view clipboard paste: %s", exc)

                if event.type() == QEvent.Type.DragEnter:
                    md = event.mimeData()
                    data_urls, file_paths = extract_attachment_sources_from_mime(md)
                    if data_urls or file_paths:
                        event.acceptProposedAction()
                        return True
                elif event.type() == QEvent.Type.Drop:
                    md = event.mimeData()
                    data_urls, file_paths = extract_attachment_sources_from_mime(md)
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
        btn.setIconSize(QSize(Icons.SIZE_NAV, Icons.SIZE_NAV))
        btn.setToolTip(tooltip)
        btn.setObjectName("toolbar_btn")
        btn.setProperty("nav", True)
        btn.setFixedSize(30, 30)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return btn

    def _create_empty_state_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("chat_empty_state")

        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 16, 28, 16)
        layout.setSpacing(10)
        layout.addStretch(1)

        hero_icon = QLabel()
        hero_icon.setObjectName("chat_empty_hero_icon")
        hero_icon.setFixedSize(40, 40)
        hero_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero_icon.setPixmap(Icons.get(Icons.PYCAT).pixmap(Icons.SIZE_EMPTY_HERO, Icons.SIZE_EMPTY_HERO))
        layout.addWidget(hero_icon, 0, Qt.AlignmentFlag.AlignHCenter)

        title = QLabel(PRODUCT_NAME)
        title.setObjectName("chat_empty_title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)
        return page

    def _update_empty_state(self) -> None:
        if not hasattr(self, "body_stack"):
            return
        has_content = bool(self._render_widgets or self._inline_question_card or self._stream.active)
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
        self._work_dir = str(path or "")
        for widget in self._render_widgets:
            widget.set_work_dir(self._work_dir)
        if not path:
            self.work_dir_btn.setIcon(Icons.get_muted(Icons.FOLDER))
            self.work_dir_btn.setText("")
            self.work_dir_btn.setProperty("workspace_state", "empty")
            self.work_dir_btn.setToolTip("未设置工作区；当前使用全局会话目录。点击设置")
        else:
            name = os.path.basename(path)
            if not name: # Root directory like C:/
                name = path
            accessible = os.path.isdir(path)
            state = "valid" if accessible else "invalid"
            color_icon = Icons.get_success(Icons.FOLDER) if accessible else Icons.get_error(Icons.FOLDER)
            self.work_dir_btn.setIcon(color_icon)
            self.work_dir_btn.setText("")
            self.work_dir_btn.setProperty("workspace_state", state)
            if accessible:
                self.work_dir_btn.setToolTip(f"工作区：{path}")
            else:
                self.work_dir_btn.setToolTip(f"工作区不可访问：{path}。点击重新选择")
        self.work_dir_btn.style().unpolish(self.work_dir_btn)
        self.work_dir_btn.style().polish(self.work_dir_btn)

    def update_header(self, model_ref: str, msg_count: int = 0) -> None:
        """Update status metadata shown in the runtime tooltip.

        The header intentionally has no second model or token display.  The
        context snapshot is projected to the Composer; model and message count
        remain discoverable from the single runtime status control.
        """
        self._header_model_ref = str(model_ref or "").strip()
        self._header_message_count = int(msg_count or 0)
        self._set_conversation_title(getattr(self._conversation, "title", ""))
        self._refresh_runtime_tooltip()

    def _set_conversation_title(self, title: object) -> None:
        text = str(title or "").strip()
        if self._conversation is not None and not text:
            text = "新会话"
        self.conversation_title_label.setText(text)
        self.conversation_title_label.setToolTip(text)
        self.conversation_title_label.setAccessibleName(
            f"当前会话：{text}" if text else ""
        )
        self.conversation_title_label.setVisible(bool(text))

    def set_model_options(self, providers: list[Provider], current_model_ref: str = "") -> None:
        return

    def update_runtime_state(
        self,
        stream_state=None,
        *,
        operation: str = "",
        pending_guidance: int = 0,
    ) -> None:
        title, detail, active = self._resolve_runtime_labels(
            stream_state,
            operation=operation,
            pending_guidance=pending_guidance,
        )
        self.runtime_indicator.setText(title)
        self.runtime_indicator.setEnabled(bool(self._conversation))
        self.runtime_indicator.setProperty("active", bool(active))
        self._runtime_detail = detail or title
        self._refresh_runtime_tooltip()
        self.runtime_indicator.style().unpolish(self.runtime_indicator)
        self.runtime_indicator.style().polish(self.runtime_indicator)

    def _refresh_runtime_tooltip(self) -> None:
        detail = str(getattr(self, "_runtime_detail", "等待下一次请求") or "等待下一次请求")
        lines = [detail]
        if self._header_model_ref:
            lines.append(f"模型：{self._header_model_ref}")
        if self._header_message_count:
            lines.append(f"消息：{self._header_message_count} 条")
        self.runtime_indicator.setToolTip("\n".join(lines))

    def show_header_notice(
        self,
        text: str,
        *,
        tone: str = "info",
        timeout_ms: int = 4000,
        conversation_id: str | None = None,
    ) -> None:
        """Show one transient notice for the currently projected conversation."""
        target = str(conversation_id or "").strip()
        current = str(getattr(self._conversation, "id", "") or "")
        if target and target != current:
            return
        value = str(text or "").strip()
        if not value:
            self.clear_header_notice()
            return
        normalized_tone = str(tone or "info").strip().lower()
        if normalized_tone not in {"info", "success", "warning", "error"}:
            normalized_tone = "info"
        self._header_notice_timer.stop()
        self.header_notice_label.setText(value)
        self.header_notice_label.setToolTip(value)
        self.header_notice_label.setAccessibleName(f"操作反馈：{value}")
        self.header_notice_label.setProperty("tone", normalized_tone)
        self.header_notice_label.setVisible(True)
        self.header_notice_label.style().unpolish(self.header_notice_label)
        self.header_notice_label.style().polish(self.header_notice_label)
        duration = max(0, int(timeout_ms or 0))
        if duration:
            self._header_notice_timer.start(duration)

    def clear_header_notice(self) -> None:
        if hasattr(self, "_header_notice_timer"):
            self._header_notice_timer.stop()
        label = getattr(self, "header_notice_label", None)
        if label is None:
            return
        label.clear()
        label.setToolTip("")
        label.setAccessibleName("")
        label.setVisible(False)
    
    def clear(self):
        self.clear_header_notice()
        self.clear_inline_question(notify=True)
        self._remove_load_earlier_button()
        while self.messages_layout.count() > 1:
            item = self.messages_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._message_widgets.clear()
        self._render_widgets.clear()
        self._message_widget_by_id.clear()
        self._message_container_by_id.clear()
        self._conversation_messages = []
        self._conversation = None
        self._header_model_ref = ""
        self._header_message_count = 0
        self._set_conversation_title("")
        self._render_start_index = 0
        self._history_window_expanded = False
        self._stream.finish()
        self.update_runtime_state(None)
        self._update_nav_state()
        self._update_empty_state()
    
    def load_conversation(self, conversation: Conversation):
        self.clear()
        self._conversation = conversation
        self._set_conversation_title(getattr(conversation, "title", ""))
        setting = (getattr(conversation, "settings", {}) or {}).get("show_thinking")
        if isinstance(setting, bool):
            self._show_thinking = setting
        self._work_dir = str(getattr(conversation, "work_dir", "") or "")
        self._conversation_messages = list(getattr(conversation, "messages", []) or [])
        total = len(self._conversation_messages)
        if RECENT_MESSAGE_WINDOW > 0:
            self._render_start_index = max(0, total - RECENT_MESSAGE_WINDOW)
        else:
            self._render_start_index = 0
        self._history_window_expanded = False
        visible_messages = self._conversation_messages[self._render_start_index :]
        self._bulk_insert_messages(visible_messages)
        self._ensure_load_earlier_button()
        self.update_runtime_state(None)
        self._update_empty_state()
        self._update_nav_state()
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _allows_external_revisions(self) -> bool:
        """Return True only for a conversation explicitly bound to a Channel."""
        return is_bound_channel_conversation(self._conversation)
    
    def add_message(self, message: Message, *, run_active: bool = False):
        message_id = str(getattr(message, "id", "") or "")
        if message_id and any(str(getattr(existing, "id", "") or "") == message_id for existing in self._conversation_messages):
            self.update_message(message)
            return

        self._conversation_messages.append(message)
        if (
            is_real_user_message(message)
            and not is_restartable_user_message(message, allow_external=self._allows_external_revisions())
        ):
            for existing_widget in self._message_widgets:
                existing_widget.set_restart_action_visible(False)
        merged_into_run = False
        if str(getattr(message, "role", "") or "") == "assistant" and len(self._conversation_messages) > 1:
            previous = self._conversation_messages[-2]
            previous_id = str(getattr(previous, "id", "") or "")
            previous_container = self._message_container_by_id.get(previous_id)
            if (
                str(getattr(previous, "role", "") or "") == "assistant"
                and previous_container is not None
                and self._render_widgets
                and previous_container is self._render_widgets[-1]
            ):
                run_messages = self._assistant_tail_messages()
                if isinstance(previous_container, AssistantRunWidget):
                    if run_active:
                        previous_container.set_active(True)
                    previous_container.set_messages(run_messages)
                    self._sync_message_widget_index()
                else:
                    self._replace_render_widget(
                        previous_container,
                        self._create_assistant_run_widget(
                            run_messages,
                            active=run_active,
                        ),
                    )
                merged_into_run = True

        if not merged_into_run:
            widget = self._create_message_widget(message)
            self._insert_above_bottom_spacer(widget)
            self._render_widgets.append(widget)
            self._sync_message_widget_index()
        self._trim_visible_tail()
        self._ensure_load_earlier_button()
        if not self._bulk_loading:
            self._update_empty_state()
            if self._should_follow_output():
                QTimer.singleShot(50, self._scroll_to_bottom)
            self._schedule_nav_update()

    def _assistant_tail_messages(self) -> list[Message]:
        result: list[Message] = []
        for message in reversed(self._conversation_messages):
            if str(getattr(message, "role", "") or "") != "assistant":
                break
            result.append(message)
        result.reverse()
        return result

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

    def update_tool_call_status(self, tool_call_id: str, detail: str) -> bool:
        target = str(tool_call_id or "").strip()
        if not target:
            return False
        for widget in reversed(self._message_widgets):
            if not widget.has_tool_call(target):
                continue
            if widget.update_tool_call_status(target, detail):
                self._schedule_nav_update()
                return True
        return False

    def append_transcript_notice(self, text: str, *, kind: str = "runtime") -> None:
        """Insert a transient UI-only runtime notice.

        These notices are intentionally not persisted as conversation messages
        and are not replayed to the model.
        """
        value = str(text or "").strip()
        if not value:
            return
        row = QFrame()
        row.setObjectName("runtime_notice")
        row.setProperty("kind", str(kind or "runtime"))
        row.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(9, 4, 9, 4)
        layout.setSpacing(6)
        label = ThemedSelectableLabel(value)
        label.setObjectName("runtime_notice_text")
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(label)
        self._insert_above_bottom_spacer(row)
        self._update_empty_state()
        if self._should_follow_output():
            QTimer.singleShot(50, self._scroll_to_bottom)
        self._schedule_nav_update()

    def _create_message_widget(self, message: Message) -> MessageWidget:
        allow_external = self._allows_external_revisions()
        allow_restart = False
        if message.role == "user":
            allow_restart = (
                restartable_user_by_id(
                    self._conversation_messages,
                    message.id,
                    allow_external=allow_external,
                )
                is not None
            )
        elif message.role == "assistant":
            allow_restart = (
                restartable_user_for_assistant(
                    self._conversation_messages,
                    message.id,
                    allow_external=allow_external,
                )
                is not None
            )
        widget = MessageWidget(
            message,
            show_thinking=self._show_thinking,
            work_dir=self._work_dir,
            artifact_lookup=self._artifact_by_name,
            content_path_resolver=self._resolve_content_path,
            delivery_refs=delivery_refs_for_messages([message]),
            allow_restart=allow_restart,
            allow_delete=not bool(getattr(message, "metadata", {}).get("synthetic"))
            if isinstance(getattr(message, "metadata", {}), dict)
            else True,
        )
        widget.reuse_requested.connect(self.reuse_message.emit)
        widget.edit_requested.connect(self.edit_message.emit)
        widget.regenerate_requested.connect(self.regenerate_message.emit)
        widget.delete_requested.connect(self.delete_message.emit)
        widget.continue_requested.connect(self.continue_message.emit)
        widget.set_revision_enabled(self._revision_enabled)
        return widget

    def set_revision_enabled(self, enabled: bool) -> None:
        self._revision_enabled = bool(enabled)
        for container in self._render_widgets:
            if isinstance(container, AssistantRunWidget):
                container.set_revision_enabled(self._revision_enabled)
            elif isinstance(container, MessageWidget):
                container.set_revision_enabled(self._revision_enabled)

    def _resolve_content_path(self, ref):
        if self._content_resolver is None or self._conversation is None:
            raise FileNotFoundError(str(getattr(ref, "ref", "") or ""))
        return self._content_resolver.resolve(self._conversation, ref)

    def _create_assistant_run_widget(
        self,
        messages: list[Message],
        *,
        active: bool = False,
    ) -> AssistantRunWidget:
        primary_message = messages[-1] if messages else None
        allow_external = self._allows_external_revisions()
        allow_restart = bool(
            primary_message is not None
            and restartable_user_for_assistant(
                self._conversation_messages,
                primary_message.id,
                allow_external=allow_external,
            )
            is not None
        )
        widget = AssistantRunWidget(
            messages,
            show_thinking=self._show_thinking,
            work_dir=self._work_dir,
            artifact_lookup=self._artifact_by_name,
            content_path_resolver=self._resolve_content_path,
            active=active,
            allow_restart=allow_restart,
        )
        widget.continue_requested.connect(self.continue_message.emit)
        widget.regenerate_requested.connect(self.regenerate_message.emit)
        widget.delete_requested.connect(self.delete_message.emit)
        widget.set_revision_enabled(self._revision_enabled)
        return widget

    def set_show_thinking(self, enabled: bool, *, refresh: bool = True) -> None:
        enabled = bool(enabled)
        if self._show_thinking == enabled:
            return
        self._show_thinking = enabled
        if refresh and self._conversation is not None:
            self.load_conversation(self._conversation)

    def _create_render_widget(self, item: SingleMessageItem | AssistantRunGroup) -> QWidget:
        if isinstance(item, AssistantRunGroup):
            return self._create_assistant_run_widget(list(item.messages))
        return self._create_message_widget(item.message)

    def _sync_message_widget_index(self) -> None:
        widgets: list[MessageWidget] = []
        by_id: dict[str, MessageWidget] = {}
        containers: dict[str, QWidget] = {}
        for container in self._render_widgets:
            children = container.message_widgets if isinstance(container, AssistantRunWidget) else [container]
            for child in children:
                if not isinstance(child, MessageWidget):
                    continue
                message_id = str(getattr(child.message, "id", "") or "")
                widgets.append(child)
                if message_id:
                    by_id[message_id] = child
                    containers[message_id] = container
        self._message_widgets = widgets
        self._message_widget_by_id = by_id
        self._message_container_by_id = containers

    def _replace_render_widget(self, old: QWidget, new: QWidget) -> None:
        render_index = self._render_widgets.index(old)
        layout_index = self.messages_layout.indexOf(old)
        self.messages_layout.removeWidget(old)
        old.deleteLater()
        self.messages_layout.insertWidget(layout_index, new, 0, Qt.AlignmentFlag.AlignTop)
        self._render_widgets[render_index] = new
        self._sync_message_widget_index()

    def _artifact_by_name(self, name: str) -> object | None:
        conversation = self._conversation
        if conversation is None:
            return None
        try:
            artifacts = dict(conversation.get_state().artifacts or {})
        except Exception:
            return None
        target = str(name or "").strip()
        if target in artifacts:
            return artifacts[target]
        for key, artifact in artifacts.items():
            if str(getattr(artifact, "name", "") or key).strip() == target:
                return artifact
        return None

    def _bulk_insert_messages(
        self,
        messages: list[Message],
        *,
        layout_index: int | None = None,
        widget_index: int | None = None,
    ) -> list[QWidget]:
        if not messages:
            return []
        created: list[QWidget] = []
        previous_updates = self.updatesEnabled()
        previous_container_updates = self.messages_container.updatesEnabled()
        self.setUpdatesEnabled(False)
        self.messages_container.setUpdatesEnabled(False)
        self._bulk_loading = True
        try:
            insert_index = self._bottom_insert_index() if layout_index is None else max(0, int(layout_index))
            render_insert_index = len(self._render_widgets) if widget_index is None else 0
            for projected_item in project_message_runs(messages):
                widget = self._create_render_widget(projected_item)
                self.messages_layout.insertWidget(insert_index, widget, 0, Qt.AlignmentFlag.AlignTop)
                self._render_widgets.insert(render_insert_index, widget)
                render_insert_index += 1
                insert_index += 1
                created.append(widget)
        finally:
            self._bulk_loading = False
            self.messages_container.setUpdatesEnabled(previous_container_updates)
            self.setUpdatesEnabled(previous_updates)
        self._sync_message_widget_index()
        return created

    def _load_earlier_messages(self) -> None:
        if self._render_start_index <= 0:
            self._ensure_load_earlier_button()
            return

        old_first_widget = self._render_widgets[0] if self._render_widgets else None
        old_first_y = old_first_widget.y() if old_first_widget is not None else 0
        scrollbar = self.scroll_area.verticalScrollBar()
        old_scroll = scrollbar.value()

        next_start = max(0, self._render_start_index - EARLIER_MESSAGE_BATCH)
        messages = self._conversation_messages[next_start : self._render_start_index]
        self._render_start_index = next_start
        self._history_window_expanded = True

        insert_index = 1 if self._load_earlier_btn is not None else 0
        self._bulk_insert_messages(messages, layout_index=insert_index, widget_index=0)
        self._ensure_load_earlier_button()
        self._update_empty_state()
        self._update_nav_state()

        if old_first_widget is not None:
            def _restore_position() -> None:
                try:
                    delta = old_first_widget.y() - old_first_y
                    scrollbar.setValue(max(0, old_scroll + delta))
                except Exception as exc:
                    logger.debug("Failed to preserve scroll position after loading history: %s", exc)

            QTimer.singleShot(0, _restore_position)

    def _ensure_load_earlier_button(self) -> None:
        remaining = max(0, int(self._render_start_index))
        if remaining <= 0:
            self._remove_load_earlier_button()
            return
        if self._load_earlier_btn is None:
            btn = QPushButton()
            btn.setObjectName("load_earlier_messages_btn")
            btn.setIcon(Icons.get_muted(Icons.CHEVRON_UP))
            btn.setIconSize(QSize(Icons.SIZE_NAV, Icons.SIZE_NAV))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(self._load_earlier_messages)
            self._load_earlier_btn = btn
            self.messages_layout.insertWidget(0, btn, 0, Qt.AlignmentFlag.AlignHCenter)
        visible = len(self._conversation_messages) - self._render_start_index
        self._load_earlier_btn.setText(f"加载更早消息 · 还有 {remaining} 条")
        self._load_earlier_btn.setToolTip(f"当前已显示最近 {visible} 条消息，点击再加载 {min(EARLIER_MESSAGE_BATCH, remaining)} 条")

    def _remove_load_earlier_button(self) -> None:
        btn = self._load_earlier_btn
        self._load_earlier_btn = None
        if btn is None:
            return
        try:
            self.messages_layout.removeWidget(btn)
        except Exception:
            pass
        btn.deleteLater()

    def _trim_visible_tail(self) -> None:
        if self._history_window_expanded or RECENT_MESSAGE_WINDOW <= 0:
            return
        next_start = max(0, len(self._conversation_messages) - RECENT_MESSAGE_WINDOW)
        if next_start != self._render_start_index:
            self._render_start_index = next_start
            self._rebuild_rendered_projection()
        self._ensure_load_earlier_button()

    def _replace_cached_message(self, message: Message) -> None:
        message_id = str(getattr(message, "id", "") or "")
        if not message_id:
            return
        for i, existing in enumerate(self._conversation_messages):
            if str(getattr(existing, "id", "") or "") == message_id:
                self._conversation_messages[i] = message
                return

    def _remove_cached_message(self, message_id: str) -> None:
        target = str(message_id or "")
        if not target:
            return
        old_len = len(self._conversation_messages)
        removed_before_window = False
        next_messages: list[Message] = []
        for i, message in enumerate(self._conversation_messages):
            if str(getattr(message, "id", "") or "") == target:
                if i < self._render_start_index:
                    removed_before_window = True
                continue
            next_messages.append(message)
        if len(next_messages) == old_len:
            return
        self._conversation_messages = next_messages
        if removed_before_window:
            self._render_start_index = max(0, self._render_start_index - 1)
        self._ensure_load_earlier_button()

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
        if self._render_widgets and isinstance(self._render_widgets[-1], AssistantRunWidget):
            self._render_widgets[-1].set_active(True)
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
        run_active = self._stream.active
        self._stream.finish()
        
        # Only add message to view if requested (to avoid duplicates)
        if add_to_view:
            self.add_message(message, run_active=run_active)
        self._update_empty_state()
        self._schedule_nav_update()

    def finish_active_run(self, status: RunStatus) -> None:
        if not self._render_widgets:
            return
        run = self._render_widgets[-1]
        if isinstance(run, AssistantRunWidget):
            run.finish(status)
    
    def is_streaming(self) -> bool:
        """Check if currently in streaming mode."""
        return self._stream.active

    def update_message(self, message: Message):
        self._replace_cached_message(message)
        message_id = str(getattr(message, "id", "") or "")
        container = self._message_container_by_id.get(message_id)
        if isinstance(container, AssistantRunWidget):
            ids = set(container.message_ids)
            run_messages = [
                existing
                for existing in self._conversation_messages
                if str(getattr(existing, "id", "") or "") in ids
            ]
            container.set_messages(run_messages)
            self._sync_message_widget_index()
            self._schedule_nav_update()
            self._update_empty_state()
            return
        if isinstance(container, MessageWidget):
            self._replace_render_widget(container, self._create_message_widget(message))
            self._schedule_nav_update()
            self._update_empty_state()
            return
        self._schedule_nav_update()
        self._update_empty_state()

    def upsert_message(self, message: Message) -> None:
        """Update a rendered message, or add it when completion only had a streaming overlay."""
        message_id = str(getattr(message, "id", "") or "")
        if not message_id:
            self.add_message(message)
            return

        if message_id in self._message_widget_by_id:
            self.update_message(message)
            return

        exists_in_cache = any(
            str(getattr(existing, "id", "") or "") == message_id
            for existing in self._conversation_messages
        )
        if exists_in_cache:
            self._replace_cached_message(message)
            self._rebuild_rendered_projection()
            self._update_empty_state()
            self._schedule_nav_update()
            return
        self.add_message(message)

    def refresh_message_tool_calls(
        self,
        message_id: str = "",
        tool_call_id: str = "",
        *,
        updated_message: Message | None = None,
    ) -> bool:
        """Refresh an existing assistant widget after a tool result is attached."""
        if updated_message is not None:
            self._replace_cached_message(updated_message)
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
        self._remove_cached_message(message_id)
        self._rebuild_rendered_projection()
        self._schedule_nav_update()
        self._update_empty_state()

    def _rebuild_rendered_projection(self) -> None:
        previous_updates = self.updatesEnabled()
        previous_container_updates = self.messages_container.updatesEnabled()
        self.setUpdatesEnabled(False)
        self.messages_container.setUpdatesEnabled(False)
        try:
            for widget in self._render_widgets:
                self.messages_layout.removeWidget(widget)
                widget.deleteLater()
            self._render_widgets = []
            self._sync_message_widget_index()
            visible = self._conversation_messages[self._render_start_index :]
            self._bulk_insert_messages(visible)
        finally:
            self.messages_container.setUpdatesEnabled(previous_container_updates)
            self.setUpdatesEnabled(previous_updates)

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

    def _navigable_widgets(self) -> List[QWidget]:
        return [w for w in self._render_widgets if w is not None]

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

    def _resolve_runtime_labels(
        self,
        stream_state,
        *,
        operation: str = "",
        pending_guidance: int = 0,
    ) -> tuple[str, str, bool]:
        operation_key = str(operation or "").strip().lower()
        if operation_key and operation_key != "turn":
            labels = {
                "prepare-input": ("准备附件", "正在创建会话附件快照"),
                "revision": ("修订中", "正在替换消息并重建活动会话历史"),
                "compact": ("压缩中", "正在压缩当前会话上下文"),
                "workspace": ("迁移中", "正在迁移当前会话文件"),
                "delete": ("删除中", "正在删除当前会话"),
            }
            title, detail = labels.get(operation_key, ("处理中", "正在处理当前会话"))
            return (title, detail, True)
        if stream_state is None:
            return ("空闲", "等待下一次请求", False)

        pending = max(0, int(pending_guidance or 0))
        active_tool = str(getattr(stream_state, "active_tool", "") or "").strip()
        last_kind = str(getattr(stream_state, "last_event_kind", "") or "").strip()
        last_detail = str(getattr(stream_state, "last_event_detail", "") or "").strip()
        if active_tool:
            title, detail = f"工具 · {active_tool}", last_detail or "正在等待工具返回"
            if pending:
                return (f"待处理 {pending}", f"{detail}；当前步骤完成后处理补充要求", True)
            return (title, detail, True)
        if last_kind:
            title, detail = self._runtime_event_label(last_kind), last_detail or "-"
            if pending:
                return (f"待处理 {pending}", f"{detail}；当前步骤完成后处理补充要求", True)
            return (title, detail, True)

        model = str(getattr(stream_state, "model", "") or "").strip()
        if pending:
            return (
                f"待处理 {pending}",
                f"{model or '正在等待模型响应'}；当前步骤完成后处理补充要求",
                True,
            )
        return ("生成中", model or "正在等待模型响应", True)

