"""
Message widget for displaying individual messages - Responsive layout
"""

import logging
from typing import Any, Callable

from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QFrame,
    QSizePolicy,
    QToolButton,
)
from PyQt6.QtCore import pyqtSignal, Qt, QTimer, QSize
from PyQt6.QtGui import QGuiApplication

from models.conversation import Message
from gui.view_models.message_tree import view_model_for_message
from gui.dialogs.image_viewer import ImageViewerDialog
from gui.utils.icon_manager import Icons
from gui.widgets.image_thumbnail import ImageThumbnail
from gui.widgets.inline_question_view import InlineQuestionCard
from gui.widgets.markdown_view import (
    MARKDOWN_CSS,
    MarkdownView,
    _normalize_markdown_for_view,
    _prepare_markdown_html_for_qt,
)
from gui.widgets.tool_call_view import (
    SubagentTraceItem,
    SubtaskRunWidget,
    ThinkingSection,
    ToolCallItem,
    ToolCallsSection,
)

logger = logging.getLogger(__name__)


MESSAGE_HEADER_HEIGHT = 22
MESSAGE_BADGE_HEIGHT = 20
MESSAGE_ACTION_SIZE = 24


class MessageWidget(QFrame):
    """Widget for displaying a single message - Compact responsive layout"""

    edit_requested = pyqtSignal(str)
    delete_requested = pyqtSignal(str)
    continue_requested = pyqtSignal(str)

    def __init__(
        self,
        message: Message,
        parent=None,
        *,
        embedded: bool = False,
        show_header: bool = True,
        show_thinking: bool = True,
        work_dir: str = "",
        artifact_lookup: Callable[[str], object | None] | None = None,
    ):
        super().__init__(parent)
        message_view = view_model_for_message(message)
        self.message_view = message_view
        self.message = message_view.message if message_view is not None else Message.from_dict(message.to_dict())
        self.embedded = bool(embedded)
        self.show_header = bool(show_header)
        self.show_thinking = bool(show_thinking)
        self.work_dir = str(work_dir or "")
        self.artifact_lookup = artifact_lookup
        self._setup_ui()

    def set_work_dir(self, work_dir: str) -> None:
        self.work_dir = str(work_dir or "")
        if hasattr(self, 'tool_calls_widget'):
            self.tool_calls_widget.set_work_dir(self.work_dir)

    def _setup_ui(self):
        is_user = self.message.role == 'user'

        # Themeable styling via QSS
        self.setObjectName("message_widget")
        self.setProperty("role", "user" if is_user else "assistant")
        self.setProperty("embedded", self.embedded)

        # Never consume vertical slack from the scroll area; blank space belongs to the viewport.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 3, 7, 3)
        layout.setSpacing(1)

        if self.show_header:
            header = QHBoxLayout()
            header.setContentsMargins(0, 0, 0, 0)
            header.setSpacing(4)
            header.setAlignment(Qt.AlignmentFlag.AlignTop)

            role_label = QLabel(("子用户" if is_user else "子助手") if self.embedded else ("你" if is_user else "助手"))
            role_label.setObjectName("message_role")
            role_label.setFixedHeight(MESSAGE_HEADER_HEIGHT)
            role_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            role_label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            header.addWidget(role_label)

            self._add_model_badge(header)
            self._add_timestamp_badge(header)
            if self.message.tokens:
                self._add_badge(header, f"T:{self.message.tokens}")
            if self.message.response_time_ms:
                self._add_badge(header, f"{self.message.response_time_ms / 1000:.1f}s")
            header.addStretch()

            actions_widget = QWidget()
            actions_layout = QHBoxLayout(actions_widget)
            actions_layout.setContentsMargins(0, 0, 0, 0)
            actions_layout.setSpacing(2)
            if not self.embedded:
                self._add_action_buttons(actions_layout)
            actions_widget.setFixedHeight(MESSAGE_ACTION_SIZE)
            actions_widget.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            header.addWidget(actions_widget)
            layout.addLayout(header)

        # Thinking (assistant only) - show above final content
        if (not is_user) and self.show_thinking and self.message.thinking:
            layout.addWidget(ThinkingSection(self.message.thinking))

        # Content
        # MarkdownView handles str conversion
        if self.message.content:
            content_view = MarkdownView(self.message.content)
            content_view.setObjectName("message_content")
            content_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            layout.addWidget(content_view)

        # Tool Calls (assistant only)
        if (not is_user) and self.message.tool_calls:
            invocations = self.message_view.tool_invocations if self.message_view is not None else self.message.tool_calls
            self.tool_calls_widget = ToolCallsSection(
                invocations,
                work_dir=self.work_dir,
                artifact_lookup=self.artifact_lookup,
            )
            layout.addWidget(self.tool_calls_widget)

        # Images
        if self.message.images:
            self._add_images(layout)

    def has_tool_call(self, tool_id: str) -> bool:
        """Check if this message contains a tool call with the given ID"""
        if not self.message.tool_calls:
            return False
        return any(tc.get('id') == tool_id for tc in self.message.tool_calls)

    def refresh_tool_calls(self):
        """Refresh tool calls display from message data"""
        if hasattr(self, 'tool_calls_widget'):
            self.tool_calls_widget.refresh_all()

    def update_tool_call_status(self, tool_id: str, detail: str) -> bool:
        if not self.has_tool_call(tool_id) or not hasattr(self, 'tool_calls_widget'):
            return False
        return bool(self.tool_calls_widget.update_status(tool_id, detail))

    def update_subtask_trace(self, trace: dict[str, Any]) -> bool:
        if not isinstance(trace, dict):
            return False
        metadata = trace.get('metadata') if isinstance(trace.get('metadata'), dict) else {}
        tool_call_id = str(
            trace.get('parent_tool_call_id')
            or trace.get('tool_call_id')
            or metadata.get('parent_tool_call_id')
            or metadata.get('tool_call_id')
            or ''
        ).strip()
        if self.message.tool_calls and tool_call_id:
            for tool_call in self.message.tool_calls:
                if str(tool_call.get('id') or '') != tool_call_id:
                    continue
                if hasattr(self, 'tool_calls_widget'):
                    self.tool_calls_widget.update_subtask(tool_call_id, trace)
                return True
        return False

    def _add_model_badge(self, layout):
        model = None
        if isinstance(self.message.metadata, dict):
            model = (
                self.message.metadata.get('model_ref')
                or self.message.metadata.get('model')
                or self.message.metadata.get('model_name')
            )
        if model:
            text = str(model)
            if len(text) > 22:
                text = text[:21] + "…"
            model_label = QLabel(text)
            model_label.setObjectName("message_badge")
            model_label.setToolTip(str(model))
            self._style_header_badge(model_label)
            layout.addWidget(model_label)

    def _add_timestamp_badge(self, layout):
        try:
            ts = self.message.created_at.strftime('%m-%d %H:%M')
            self._add_badge(layout, ts)
        except Exception as exc:
            logger.debug("Failed to format message timestamp badge: %s", exc)

    def _add_badge(self, layout, text):
        label = QLabel(text)
        label.setObjectName("message_badge")
        self._style_header_badge(label)
        layout.addWidget(label)

    @staticmethod
    def _style_header_badge(label: QLabel) -> None:
        label.setFixedHeight(MESSAGE_BADGE_HEIGHT)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def _add_action_buttons(self, layout):
        metadata = self.message.metadata if isinstance(self.message.metadata, dict) else {}
        if (
            self.message.role == "assistant"
            and bool(metadata.get("interrupted"))
            and not bool(metadata.get("resume_requested"))
        ):
            continue_btn = QToolButton()
            continue_btn.setIcon(Icons.get(Icons.PLAY))
            continue_btn.setIconSize(QSize(18, 18))
            continue_btn.setToolTip("继续未完成的 Agent 运行")
            continue_btn.setFixedSize(MESSAGE_ACTION_SIZE, MESSAGE_ACTION_SIZE)
            continue_btn.setObjectName("msg_continue_btn")
            continue_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            continue_btn.clicked.connect(lambda: self.continue_requested.emit(self.message.id))
            layout.addWidget(continue_btn)

        copy_btn = QToolButton()
        copy_btn.setIcon(Icons.get_muted(Icons.COPY))
        copy_btn.setIconSize(QSize(18, 18))
        copy_btn.setToolTip("复制原文")
        copy_btn.setFixedSize(MESSAGE_ACTION_SIZE, MESSAGE_ACTION_SIZE)
        copy_btn.setObjectName("msg_copy_btn")
        copy_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        copy_btn.clicked.connect(self._copy_original_content)
        self._copy_btn = copy_btn
        layout.addWidget(copy_btn)

        edit_btn = QToolButton()
        edit_btn.setIcon(Icons.get_muted(Icons.EDIT))
        edit_btn.setIconSize(QSize(18, 18))
        edit_btn.setToolTip("编辑")
        edit_btn.setFixedSize(MESSAGE_ACTION_SIZE, MESSAGE_ACTION_SIZE)
        edit_btn.setObjectName("msg_edit_btn")
        edit_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        edit_btn.clicked.connect(lambda: self.edit_requested.emit(self.message.id))
        layout.addWidget(edit_btn)

        delete_btn = QToolButton()
        delete_btn.setIcon(Icons.get_error(Icons.TRASH))
        delete_btn.setIconSize(QSize(18, 18))
        delete_btn.setToolTip("删除")
        delete_btn.setFixedSize(MESSAGE_ACTION_SIZE, MESSAGE_ACTION_SIZE)
        delete_btn.setObjectName("msg_delete_btn")
        delete_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        delete_btn.clicked.connect(lambda: self.delete_requested.emit(self.message.id))
        layout.addWidget(delete_btn)

    def _add_images(self, layout):
        images_layout = QHBoxLayout()
        images_layout.setSpacing(4)
        for image_data in self.message.images[:4]:
            thumb = ImageThumbnail(image_data)
            thumb.clicked.connect(lambda _=None, d=image_data: self._open_image_preview(d))
            images_layout.addWidget(thumb)
        images_layout.addStretch()
        layout.addLayout(images_layout)

    def _copy_original_content(self) -> None:
        text = str(self.message.content or "")
        QGuiApplication.clipboard().setText(text)

        if not hasattr(self, "_copy_btn"):
            return

        try:
            self._copy_btn.setToolTip("已复制")
            QTimer.singleShot(1200, self._restore_copy_btn_tooltip)
        except RuntimeError:
            pass

    def _restore_copy_btn_tooltip(self):
        try:
            self._copy_btn.setToolTip("复制原文")
        except RuntimeError:
            pass

    def _open_image_preview(self, image_data: str):
        dialog = ImageViewerDialog(image_data, self)
        dialog.exec()
