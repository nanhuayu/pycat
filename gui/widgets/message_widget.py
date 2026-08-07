"""
Message widget for displaying individual messages - Responsive layout
"""

import logging
from typing import Any, Callable, Iterable

from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QFrame,
    QMenu,
    QSizePolicy,
    QToolButton,
)
from PyQt6.QtCore import pyqtSignal, Qt, QTimer, QSize
from PyQt6.QtGui import QAction, QGuiApplication

from core.context.history import is_real_user_message
from models.conversation import Message
from models.contracts.content import ContentRef
from gui.view_models.message_tree import view_model_for_message
from gui.dialogs.image_viewer import ImageViewerDialog
from gui.utils.icon_manager import Icons
from gui.utils.theme import prepare_context_menu
from gui.widgets.image_thumbnail import ImageThumbnail
from gui.widgets.inline_question_view import InlineQuestionCard
from gui.widgets.markdown_view import (
    MARKDOWN_CSS,
    MarkdownView,
    _normalize_markdown_for_view,
    _prepare_markdown_html_for_qt,
)
from gui.widgets.tool_call_view import (
    SubtaskRunWidget,
    ThinkingSection,
    ToolCallItem,
    ToolCallsSection,
)
from gui.widgets.workflow_capsule import WorkflowCapsuleRow

logger = logging.getLogger(__name__)


MESSAGE_HEADER_HEIGHT = 22
MESSAGE_BADGE_HEIGHT = 20
MESSAGE_ACTION_SIZE = 24


class MessageWidget(QFrame):
    """Widget for displaying a single message - Compact responsive layout"""

    reuse_requested = pyqtSignal(str)
    edit_requested = pyqtSignal(str)
    regenerate_requested = pyqtSignal(str)
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
        content_path_resolver: Callable[[ContentRef], object] | None = None,
        delivery_refs: Iterable[ContentRef] = (),
        allow_restart: bool = True,
        allow_delete: bool = True,
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
        self.content_path_resolver = content_path_resolver
        self.delivery_refs = list(delivery_refs or ())
        self.allow_restart = bool(allow_restart)
        self.allow_delete = bool(allow_delete)
        self._revision_enabled = True
        self._setup_ui()

    @staticmethod
    def create_embedded(
        message: Message,
        *,
        work_dir: str = "",
        artifact_lookup: Callable[[str], object | None] | None = None,
    ) -> "MessageWidget":
        """Factory injected into subtask widgets so tool_call_view never imports this module."""
        return MessageWidget(message, embedded=True, work_dir=work_dir, artifact_lookup=artifact_lookup)

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
        if not self.embedded and (self._is_editable_user_message() or self._can_delete_message()):
            self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self.customContextMenuRequested.connect(self._show_message_menu)

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
            self.thinking_widget = ThinkingSection(self.message.thinking)
            layout.addWidget(self.thinking_widget)

        # Content
        # MarkdownView handles str conversion
        if self.message.content:
            content_view = MarkdownView(self.message.content)
            content_view.setObjectName("message_content")
            content_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            layout.addWidget(content_view)

        if self.message.content_refs:
            self._add_content_refs(layout)

        # Tool Calls (assistant only)
        if (not is_user) and self.message.tool_calls:
            invocations = self.message_view.tool_invocations if self.message_view is not None else self.message.tool_calls
            self.tool_calls_widget = ToolCallsSection(
                invocations,
                work_dir=self.work_dir,
                artifact_lookup=self.artifact_lookup,
                embedded_message_factory=MessageWidget.create_embedded,
            )
            layout.addWidget(self.tool_calls_widget)

        # Images
        if self.message.images:
            self._add_images(layout)

        if (not is_user) and self.delivery_refs:
            self._add_delivery_refs(layout)

    def has_tool_call(self, tool_id: str) -> bool:
        """Check if this message contains a tool call with the given ID"""
        if not self.message.tool_calls:
            return False
        return any(tc.get('id') == tool_id for tc in self.message.tool_calls)

    def refresh_tool_calls(self):
        """Refresh tool calls display from message data"""
        if hasattr(self, 'tool_calls_widget'):
            self.tool_calls_widget.refresh_all()

    def expansion_state(self) -> dict[str, object]:
        state: dict[str, object] = {}
        thinking = getattr(self, "thinking_widget", None)
        if thinking is not None:
            state["thinking"] = bool(thinking.is_expanded)
        tools = getattr(self, "tool_calls_widget", None)
        if tools is not None:
            state["tools"] = tools.expanded_tool_ids()
        return state

    def restore_expansion_state(self, state: dict[str, object] | None) -> None:
        values = state or {}
        thinking = getattr(self, "thinking_widget", None)
        if thinking is not None and "thinking" in values:
            thinking.set_expanded(bool(values["thinking"]))
        tools = getattr(self, "tool_calls_widget", None)
        if tools is not None:
            tools.restore_expanded_tool_ids(values.get("tools") or ())

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

        if self._is_editable_user_message():
            edit_btn = QToolButton()
            edit_btn.setIcon(Icons.get_muted(Icons.EDIT))
            edit_btn.setIconSize(QSize(18, 18))
            edit_btn.setToolTip("编辑并重试；发送后移除该轮原回复和后续对话")
            edit_btn.setFixedSize(MESSAGE_ACTION_SIZE, MESSAGE_ACTION_SIZE)
            edit_btn.setObjectName("msg_edit_btn")
            edit_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            edit_btn.clicked.connect(lambda: self.edit_requested.emit(self.message.id))
            edit_btn.setEnabled(self._revision_enabled)
            self._edit_btn = edit_btn
            layout.addWidget(edit_btn)
        elif self.message.role == "assistant" and self.allow_restart:
            regenerate_btn = QToolButton()
            regenerate_btn.setIcon(Icons.get_muted(Icons.REFRESH))
            regenerate_btn.setIconSize(QSize(18, 18))
            regenerate_btn.setToolTip("重新生成；移除该轮回复和后续对话，已执行的操作不会撤销")
            regenerate_btn.setFixedSize(MESSAGE_ACTION_SIZE, MESSAGE_ACTION_SIZE)
            regenerate_btn.setObjectName("msg_regenerate_btn")
            regenerate_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            regenerate_btn.clicked.connect(
                lambda: self.regenerate_requested.emit(self.message.id)
            )
            regenerate_btn.setEnabled(self._revision_enabled)
            self._regenerate_btn = regenerate_btn
            layout.addWidget(regenerate_btn)

    def _is_editable_user_message(self) -> bool:
        return self.allow_restart and is_real_user_message(self.message)

    def _can_delete_message(self) -> bool:
        if not self.allow_delete or not self.allow_restart:
            return False
        if self.message.role not in {"user", "assistant"}:
            return False
        metadata = self.message.metadata if isinstance(self.message.metadata, dict) else {}
        return not bool(metadata.get("synthetic"))

    def set_revision_enabled(self, enabled: bool) -> None:
        self._revision_enabled = bool(enabled)
        button = getattr(self, "_edit_btn", None)
        if button is not None:
            button.setEnabled(self._revision_enabled)
        regenerate_button = getattr(self, "_regenerate_btn", None)
        if regenerate_button is not None:
            regenerate_button.setEnabled(self._revision_enabled)

    def set_restart_action_visible(self, visible: bool) -> None:
        self.allow_restart = bool(visible)
        for name in ("_edit_btn", "_regenerate_btn"):
            button = getattr(self, name, None)
            if button is not None:
                button.setVisible(bool(visible))

    def create_message_menu(self) -> QMenu:
        menu = prepare_context_menu(QMenu(self), self)
        if self.message.role == "user" and is_real_user_message(self.message):
            reuse_action = QAction("复用到输入框（不修改历史）", menu)
            reuse_action.triggered.connect(lambda: self.reuse_requested.emit(self.message.id))
            menu.addAction(reuse_action)
        if self._can_delete_message():
            if menu.actions():
                menu.addSeparator()
            delete_action = QAction("删除该轮及后续对话", menu)
            delete_action.setToolTip("只修改 PyCat 记录，不撤销已经执行的操作")
            delete_action.triggered.connect(lambda: self.delete_requested.emit(self.message.id))
            menu.addAction(delete_action)
        return menu

    def _show_message_menu(self, position) -> None:
        if not (self._is_editable_user_message() or self._can_delete_message()):
            return
        self.create_message_menu().exec(self.mapToGlobal(position))

    def _add_images(self, layout):
        images_layout = QHBoxLayout()
        images_layout.setSpacing(4)
        for image_data in self.message.images[:4]:
            thumb = ImageThumbnail(image_data)
            thumb.clicked.connect(lambda _=None, d=image_data: self._open_image_preview(d))
            images_layout.addWidget(thumb)
        images_layout.addStretch()
        layout.addLayout(images_layout)

    def _add_content_refs(self, layout) -> None:
        for ref in self.message.content_refs:
            path = None
            if self.content_path_resolver is not None:
                try:
                    path = self.content_path_resolver(ref)
                except Exception as exc:
                    logger.debug("Failed to resolve message content %s: %s", ref.ref, exc)
            if str(ref.mime or "").startswith("image/") and path is not None:
                source = str(path)
                thumb = ImageThumbnail(source)
                thumb.setToolTip(f"{ref.name}\n{ref.mime} · {ref.size} bytes")
                thumb.clicked.connect(lambda _=None, image_source=source: self._open_image_preview(image_source))
                layout.addWidget(thumb, 0, Qt.AlignmentFlag.AlignLeft)
                continue

            row = WorkflowCapsuleRow(
                kind="input",
                status="completed" if path is not None else "failed",
                payload=ref,
                file_path=str(path or ""),
                work_dir=self.work_dir,
            )
            row.set_content(
                icon=Icons.get_muted(Icons.FILE_LINES),
                title=ref.name,
                meta=f"{ref.mime} · {ref.size} B",
            )
            row.setToolTip(f"{ref.name}\n{ref.ref}\n{ref.mime} · {ref.size} bytes")
            row.set_interactive(path is not None)
            row.clicked.connect(lambda _payload, capsule=row: capsule.open_file())
            layout.addWidget(row)

    def _add_delivery_refs(self, layout) -> None:
        title = QLabel(f"本次产出 · {len(self.delivery_refs)}")
        title.setObjectName("delivery_section_title")
        title.setProperty("muted", True)
        layout.addWidget(title)

        for ref in self.delivery_refs[:4]:
            path = None
            if self.content_path_resolver is not None:
                try:
                    path = self.content_path_resolver(ref)
                except Exception as exc:
                    logger.debug("Failed to resolve delivered content %s: %s", ref.ref, exc)
            if str(ref.mime or "").startswith("image/") and path is not None:
                source = str(path)
                thumb = ImageThumbnail(source)
                thumb.setToolTip(f"{ref.name}\n{ref.mime} · {ref.size} bytes")
                thumb.clicked.connect(
                    lambda _=None, image_source=source: self._open_image_preview(image_source)
                )
                layout.addWidget(thumb, 0, Qt.AlignmentFlag.AlignLeft)
                continue

            row = WorkflowCapsuleRow(
                kind="output",
                status="completed" if path is not None else "failed",
                payload=ref,
                file_path=str(path or ""),
                work_dir=self.work_dir,
            )
            row.set_content(
                icon=Icons.get_muted(Icons.FILE_LINES),
                title=ref.name,
                meta=self._format_file_size(ref.size),
            )
            row.setToolTip(
                f"{ref.name}\n{ref.ref}\n{ref.mime} · {ref.size} bytes"
                + ("\n点击打开" if path is not None else "\n文件不可用")
            )
            row.set_interactive(path is not None)
            row.clicked.connect(lambda _payload, capsule=row: capsule.open_file())
            layout.addWidget(row)

        remaining = len(self.delivery_refs) - 4
        if remaining > 0:
            more = QLabel(f"另有 {remaining} 项，可在右侧内容中查看")
            more.setObjectName("delivery_more_label")
            more.setProperty("muted", True)
            layout.addWidget(more)

    @staticmethod
    def _format_file_size(size: int) -> str:
        value = max(0, int(size or 0))
        if value < 1024:
            return f"{value} B"
        if value < 1024 * 1024:
            return f"{value / 1024:.1f} KB"
        return f"{value / (1024 * 1024):.1f} MB"

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
