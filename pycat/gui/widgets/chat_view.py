"""
Chat view widget - Compact responsive layout
"""

import logging
import os
from typing import List

from PyQt6.QtCore import QEvent, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pycat.core.channel.bindings import is_bound_channel_conversation
from pycat.core.content.references import delivery_refs_for_messages
from pycat.core.context.history import (
    is_real_user_message,
    is_restartable_user_message,
    restartable_user_by_id,
    restartable_user_for_assistant,
)
from pycat.gui.about_content import PRODUCT_NAME
from pycat.gui.runtime.content_navigation import ContentOpenUseCase
from pycat.gui.shortcuts import shortcut_sequence
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.image_utils import (
    extract_attachment_sources_from_clipboard,
    extract_attachment_sources_from_mime,
)
from pycat.gui.view_models.message_runs import (
    AssistantRunGroup,
    SingleMessageItem,
    project_message_runs,
)
from pycat.gui.widgets.themed_line_edit import ThemedSelectableLabel
from pycat.models.contracts.agent import RunStatus
from pycat.models.conversation import Conversation, Message
from pycat.models.provider import Provider
from pycat.models.workspace import WorkspaceLocation

from .assistant_run_widget import AssistantRunWidget
from .capsule import SingleLineLabel
from .chat.streaming_overlay import StreamingOverlay
from .message_widget import MessageWidget
from .question_form import QuestionForm

logger = logging.getLogger(__name__)


class ChatView(QWidget):
    """Scrollable view for displaying chat messages"""
    
    reuse_message = pyqtSignal(str)
    edit_message = pyqtSignal(str)
    regenerate_message = pyqtSignal(str)
    delete_message = pyqtSignal(str)
    continue_message = pyqtSignal(str)
    images_dropped = pyqtSignal(list)
    image_edit_requested = pyqtSignal(str)
    workspace_requested = pyqtSignal()
    attach_requested = pyqtSignal()
    model_requested = pyqtSignal()
    trace_requested = pyqtSignal()
    run_trace_requested = pyqtSignal(object)
    conversation_changed = pyqtSignal()
    
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
        self._content_open = ContentOpenUseCase(content_service) if content_service is not None else None
        self._show_thinking = True
        self._work_dir = ""
        self._header_model_ref = ""
        self._header_message_count = 0
        self._runtime_detail = "等待下一次请求"
        self._bulk_loading = False
        self._inline_question_card: QuestionForm | None = None
        self._nav_update_timer: QTimer | None = None
        self._stream = StreamingOverlay(scroll_area=None, should_auto_scroll=self._should_follow_output)  # scroll_area set after _setup_ui
        self._follow_output = True
        self._revision_enabled = True
        
        self._setup_ui()
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(self.clear_notice)
        self._stream._scroll_area = self.scroll_area
        self._stream.setParent(self.scroll_area)
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # ===== Header bar with model indicator =====
        self.header_bar = QFrame()
        self.header_bar.setObjectName("chat_header")
        self.header_bar.setFixedHeight(48)
        
        header_layout = QHBoxLayout(self.header_bar)
        header_layout.setContentsMargins(16, 6, 8, 6)
        header_layout.setSpacing(6)
        
        # ===== Workspace/Folder Button =====
        self.work_dir_btn = QPushButton()
        self.work_dir_btn.setIcon(Icons.get_muted(Icons.FOLDER))
        self.work_dir_btn.setText("个人空间")
        self.work_dir_btn.setObjectName("work_dir_btn")
        self.work_dir_btn.setAccessibleName("工作区")
        self.work_dir_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.work_dir_btn.setToolTip("未设置工作区；只读工具以用户目录为默认范围。点击设置")
        self.work_dir_btn.setIconSize(QSize(Icons.SIZE_NAV, Icons.SIZE_NAV))
        self.work_dir_btn.setFixedHeight(30)
        self.work_dir_btn.setMaximumWidth(160)
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

        self.conversation_title_label = SingleLineLabel(parent=self.header_bar, max_characters=36)
        self.conversation_title_label.setObjectName("conversation_title_label")
        self.conversation_title_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.conversation_title_label.setMinimumWidth(0)
        self.conversation_title_label.setFixedHeight(30)
        self.conversation_title_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        header_layout.addWidget(self.conversation_title_label, 1)

        self.runtime_indicator = QToolButton()
        self.runtime_indicator.setObjectName("runtime_indicator")
        self.runtime_indicator.setText("运行检查")
        self.runtime_indicator.setIcon(Icons.get_muted(Icons.CHART_BARS))
        self.runtime_indicator.setProperty("active", False)
        self.runtime_indicator.setToolTip("等待下一次请求")
        self.runtime_indicator.setAccessibleName("运行状态与调用链路")
        self.runtime_indicator.setCursor(Qt.CursorShape.PointingHandCursor)
        self.runtime_indicator.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.runtime_indicator.setFixedHeight(30)
        self.runtime_indicator.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.runtime_indicator.clicked.connect(self.trace_requested.emit)
        header_layout.addWidget(self.runtime_indicator)

        # MainWindow places this one conversation-filtered feedback slot in the
        # composer footer; the timer stays owned by the conversation view.
        self.status_notice = SingleLineLabel(parent=self)
        self.status_notice.setObjectName("status_notice")
        self.status_notice.setMinimumWidth(0)
        self.status_notice.setFixedHeight(24)
        self.status_notice.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.status_notice.setVisible(False)

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
        self.messages_layout.setContentsMargins(24, 20, 24, 12)
        self.messages_layout.setSpacing(16)
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

    def resizeEvent(self, event):
        super().resizeEvent(event)
        inset = max(20, (self.width() - 820) // 2)
        self.messages_layout.setContentsMargins(inset, 20, inset, 12)

    def _create_nav_bar(self) -> QWidget:
        nav_group = QFrame()
        nav_group.setObjectName("chat_nav_group")
        nav_group.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        nav_layout = QHBoxLayout(nav_group)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(2)


        self.nav_prev_btn = self._create_nav_button(Icons.CHEVRON_UP, "上一条消息")
        self.nav_prev_btn.clicked.connect(self.go_prev_message)
        nav_layout.addWidget(self.nav_prev_btn)

        self.nav_next_btn = self._create_nav_button(Icons.CHEVRON_DOWN, "下一条消息")
        self.nav_next_btn.clicked.connect(self.go_next_message)
        nav_layout.addWidget(self.nav_next_btn)

        self._message_shortcuts = {}
        for key, callback in (("message_previous", self.go_prev_message), ("message_next", self.go_next_message),
                              ("message_first", self._scroll_to_top), ("message_last", self._scroll_to_bottom)):
            shortcut = QShortcut(QKeySequence(shortcut_sequence(key)), self)
            shortcut.activated.connect(callback)
            self._message_shortcuts[key] = shortcut
        
        return nav_group

    def _create_nav_button(self, icon_name: str, tooltip: str) -> QToolButton:
        btn = QToolButton(self)
        btn.setIcon(Icons.get_muted(icon_name))
        btn.setIconSize(QSize(Icons.SIZE_NAV, Icons.SIZE_NAV))
        btn.setToolTip(tooltip)
        btn.setObjectName("toolbar_btn")
        btn.setProperty("nav", True)
        btn.setFixedSize(30, 30)
        btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        return btn

    def refresh_theme(self):
        self.update_work_dir(self._work_dir)
        self.runtime_indicator.setIcon(Icons.get_muted(Icons.CHART_BARS))
        for button, icon in ((self.nav_prev_btn, Icons.CHEVRON_UP), (self.nav_next_btn, Icons.CHEVRON_DOWN)):
            button.setIcon(Icons.get_muted(icon))
        for widget in self._render_widgets:
            if isinstance(widget, AssistantRunWidget):
                widget._sync_process_visibility()
            for button in widget.findChildren(QToolButton):
                icon = {"msg_copy_btn": Icons.COPY, "msg_edit_btn": Icons.EDIT, "msg_regenerate_btn": Icons.REFRESH, "msg_continue_btn": Icons.PLAY}.get(button.objectName())
                if icon:
                    button.setIcon(Icons.get_muted(icon))

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
        hero_icon.setPixmap(Icons.brand().pixmap(32, 32))
        heading = QHBoxLayout()
        heading.addStretch()
        heading.addWidget(hero_icon)

        title = QLabel(f"和 {PRODUCT_NAME} 开始对话")
        title.setObjectName("chat_empty_title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading.addWidget(title)
        heading.addStretch()
        layout.addLayout(heading)
        hint = QLabel("写下目标，或添加资料开始。")
        hint.setProperty("muted", True)
        layout.addWidget(hint, 0, Qt.AlignmentFlag.AlignHCenter)
        actions = QHBoxLayout()
        actions.setSpacing(6)
        actions.addStretch()
        for label, icon, signal in (("选择工作区", Icons.FOLDER, self.workspace_requested),
                                    ("添加图片或文件", Icons.PLUS, self.attach_requested),
                                    ("选择模型", Icons.MODEL, self.model_requested)):
            button = QPushButton(label)
            button.setIcon(Icons.get_muted(icon))
            button.clicked.connect(signal.emit)
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        layout.addStretch(1)
        return page

    def _update_empty_state(self) -> None:
        if not hasattr(self, "body_stack"):
            return
        has_content = bool(self._render_widgets or self._inline_question_card or self._stream.active)
        self.body_stack.setCurrentWidget(self.scroll_area if has_content else self.empty_state_page)
    
    def _select_work_dir(self):
        self.workspace_requested.emit()

    def update_work_dir(self, path: str):
        """Update workspace directory display"""
        self._work_dir = str(path or "")
        for widget in self._render_widgets:
            widget.set_work_dir(self._work_dir)
        if not path:
            self.work_dir_btn.setIcon(Icons.get_muted(Icons.FOLDER))
            self.work_dir_btn.setText("个人空间")
            self.work_dir_btn.setProperty("workspace_state", "empty")
            self.work_dir_btn.setToolTip("未设置工作区；只读工具以用户目录为默认范围。点击设置")
        else:
            location = WorkspaceLocation.parse(path)
            name = location.label
            if not name: # Root directory like C:/
                name = path
            accessible = location.is_remote or os.path.isdir(path)
            state = "valid" if accessible else "invalid"
            color_icon = Icons.get_success(Icons.FOLDER) if accessible else Icons.get_error(Icons.FOLDER)
            self.work_dir_btn.setIcon(color_icon)
            self.work_dir_btn.setText(self.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, 112))
            self.work_dir_btn.setProperty("workspace_state", state)
            if location.is_remote:
                self.work_dir_btn.setToolTip(f"SSH 工作区：{location.endpoint}:{location.root}\n运行前检查连接；点击重新连接或选择目录")
            elif accessible:
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
        self.conversation_title_label.setVisible(True)

    def set_model_options(self, providers: list[Provider], current_model_ref: str = "") -> None:
        return

    def update_runtime_state(
        self,
        stream_state=None,
        *,
        operation: str = "",
        pending_guidance: int = 0,
        pending_interactions: int = 0,
    ) -> None:
        title, detail, active = self._resolve_runtime_labels(
            stream_state,
            operation=operation,
            pending_guidance=pending_guidance,
        )
        if pending_interactions:
            title = "等待回复"
            detail = f"此会话有 {pending_interactions} 项问题或工具确认等待你处理。"
            active = True
        self.runtime_indicator.setText(title if active else "运行检查")
        self.runtime_indicator.setEnabled(bool(self._conversation))
        self.runtime_indicator.setProperty("active", bool(active))
        self._runtime_detail = detail or title
        if active:
            self._stream.set_waiting_hint(title, detail)
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

    def show_notice(
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
            self.clear_notice()
            return
        normalized_tone = str(tone or "info").strip().lower()
        if normalized_tone not in {"info", "success", "warning", "error"}:
            normalized_tone = "info"
        self._notice_timer.stop()
        self.status_notice.setText(value)
        self.status_notice.setToolTip(value)
        self.status_notice.setAccessibleName(f"操作反馈：{value}")
        self.status_notice.setProperty("tone", normalized_tone)
        self.status_notice.setVisible(True)
        self.status_notice.style().unpolish(self.status_notice)
        self.status_notice.style().polish(self.status_notice)
        duration = max(0, int(timeout_ms or 0))
        if duration:
            self._notice_timer.start(duration)

    def clear_notice(self) -> None:
        if hasattr(self, "_notice_timer"):
            self._notice_timer.stop()
        label = getattr(self, "status_notice", None)
        if label is None:
            return
        label.clear()
        label.setToolTip("")
        label.setAccessibleName("")
        label.setVisible(False)
    
    def clear(self):
        self.clear_notice()
        self.set_inline_question(None)
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
        self._stream.finish()
        self.update_runtime_state(None)
        self._update_nav_state()
        self._update_empty_state()
        self.conversation_changed.emit()
    
    def load_conversation(self, conversation: Conversation):
        self.clear()
        self._conversation = conversation
        self._set_conversation_title(getattr(conversation, "title", ""))
        setting = (getattr(conversation, "settings", {}) or {}).get("show_thinking")
        if isinstance(setting, bool):
            self._show_thinking = setting
        self._work_dir = str(getattr(conversation, "work_dir", "") or "")
        self._conversation_messages = list(getattr(conversation, "messages", []) or [])
        self._bulk_insert_messages(self._conversation_messages)
        self.update_runtime_state(None)
        self._update_empty_state()
        self._update_nav_state()
        QTimer.singleShot(0, self._scroll_to_bottom)
        self.conversation_changed.emit()

    @property
    def conversation_id(self) -> str:
        return self._conversation.id if self._conversation is not None else ""

    def bind_conversation(self, conversation: Conversation) -> None:
        """Bind a replacement snapshot before rendering incremental messages."""
        if self.conversation_id != conversation.id:
            self.load_conversation(conversation)
        else:
            self._conversation = conversation

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
            content_target_resolver=self._resolve_content_target,
            delivery_refs=delivery_refs_for_messages([message]),
            allow_restart=allow_restart,
            allow_delete=not bool(getattr(message, "metadata", {}).get("synthetic"))
            if isinstance(getattr(message, "metadata", {}), dict)
            else True,
        )
        widget.reuse_requested.connect(self.reuse_message.emit)
        widget.image_edit_requested.connect(self.image_edit_requested.emit)
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

    def _resolve_content_target(self, ref, *, verify_integrity: bool):
        if self._content_open is None or self._conversation is None:
            raise FileNotFoundError(str(getattr(ref, "ref", "") or ""))
        return self._content_open.resolve(
            self._conversation,
            ref,
            verify_integrity=verify_integrity,
        )

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
            content_target_resolver=self._resolve_content_target,
            active=active,
            allow_restart=allow_restart,
        )
        widget.continue_requested.connect(self.continue_message.emit)
        widget.trace_requested.connect(self.run_trace_requested.emit)
        widget.image_edit_requested.connect(self.image_edit_requested.emit)
        widget.projection_changed.connect(self._sync_message_widget_index)
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
            if isinstance(container, AssistantRunWidget):
                containers.update({message_id: container for message_id in container.message_ids})
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
        next_messages: list[Message] = [
            message
            for message in self._conversation_messages
            if str(getattr(message, "id", "") or "") != target
        ]
        if len(next_messages) == old_len:
            return
        self._conversation_messages = next_messages

    def set_inline_question(self, card: QuestionForm | None) -> None:
        """Mount a presenter-owned form; navigating never answers or destroys it."""
        if self._inline_question_card is card:
            return
        previous = self._inline_question_card
        if previous is not None:
            self.messages_layout.removeWidget(previous)
            previous.hide()
        self._inline_question_card = card
        if card is not None:
            self._insert_above_bottom_spacer(card)
            card.show()
            if self._should_follow_output():
                QTimer.singleShot(50, self._scroll_to_bottom)
        self._update_empty_state()
        self._schedule_nav_update()
    
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
            if updated_message.id in self._message_container_by_id:
                self.update_message(updated_message)
                return True
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
            visible = self._conversation_messages
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
            self.nav_prev_btn.setEnabled(False)
            self.nav_next_btn.setEnabled(False)
            
            self.nav_prev_btn.setToolTip("上一条消息")
            self.nav_next_btn.setToolTip("下一条消息")
            return

        idx = self._find_current_message_index()
        if idx < 0:
            idx = 0

        self.nav_prev_btn.setEnabled(idx > 0)
        self.nav_next_btn.setEnabled(idx < total - 1)

        # Keep the UI minimal: show position in tooltips instead of an always-visible counter.
        pos_text = f"{idx + 1}/{total}"
        self.nav_prev_btn.setToolTip(f"上一条消息 ({pos_text})")
        self.nav_next_btn.setToolTip(f"下一条消息 ({pos_text})")
    
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
