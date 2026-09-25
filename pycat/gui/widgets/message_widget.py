"""
Message widget for displaying individual messages - Responsive layout
"""

import logging
from functools import partial
from typing import Any, Callable, Iterable

from PyQt6.QtCore import QCoreApplication, QEvent, QPoint, QSize, Qt, QTimer, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QAction, QDesktopServices, QGuiApplication
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pycat.core.context.history import is_real_user_message
from pycat.gui.dialogs.content_preview import show_content
from pycat.gui.runtime.content_navigation import ContentOpenTarget, ContentOpenUseCase, ContentTargetResolver
from pycat.gui.utils.display_text import message_time
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.image_loader import read_image
from pycat.gui.utils.message_images import image_key, image_source, message_image_markdown
from pycat.gui.utils.theme import prepare_context_menu
from pycat.gui.view_models.message_tree import view_model_for_message
from pycat.gui.widgets.capsule import SingleLineLabel
from pycat.gui.widgets.content_ref_widget import ContentRefWidget
from pycat.gui.widgets.delivery_group import DeliveryGroup
from pycat.gui.widgets.image_thumbnail import ImageThumbnail
from pycat.gui.widgets.markdown_view import MarkdownView
from pycat.gui.widgets.themed_line_edit import ThemedSelectableLabel
from pycat.gui.widgets.tool_call_view import (
    ThinkingSection,
    ToolCallsSection,
)
from pycat.models.contracts.content import ContentRef
from pycat.models.conversation import Message

logger = logging.getLogger(__name__)


def _read_message_image(target: ContentOpenTarget):
    target = ContentOpenUseCase.prepare_target(target)
    return read_image(str(target.path), max_size=QSize(1280, 960))


MESSAGE_HEADER_HEIGHT = 22
MESSAGE_BADGE_HEIGHT = 20
MESSAGE_ACTION_SIZE = 24


class MessageActionButton(QToolButton):
    """Keep the keyboard target without an offscreen opacity surface."""
    revealed = False

    def __init__(self, parent=None, *, persistent: bool = False):
        super().__init__(parent)
        self._persistent = persistent
        self.setProperty("messageAction", True)
        self.setAutoRaise(True)

    def paintEvent(self, event):
        if self._persistent or self.revealed or self.hasFocus() or self.underMouse():
            super().paintEvent(event)


class MessageWidget(QFrame):
    """Widget for displaying a single message - Compact responsive layout"""

    reuse_requested = pyqtSignal(str)
    edit_requested = pyqtSignal(str)
    regenerate_requested = pyqtSignal(str)
    delete_requested = pyqtSignal(str)
    continue_requested = pyqtSignal(str)
    image_edit_requested = pyqtSignal(str)

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
        content_target_resolver: ContentTargetResolver | None = None,
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
        self.content_target_resolver = content_target_resolver
        self.delivery_refs = list(delivery_refs or ())
        self._inline_images = set()
        self._image_aliases = {}
        self._image_actions = {}
        self.allow_restart = bool(allow_restart)
        self.allow_delete = bool(allow_delete)
        self._revision_enabled = True
        self._focus_timer = QTimer(self)
        self._focus_timer.setSingleShot(True)
        self._focus_timer.timeout.connect(self._sync_action_visibility)
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
        is_cancellation_notice = (
            self.message.role == 'assistant'
            and self.message.metadata.get('runtime_error')
            and self.message.metadata.get('run_status') == 'cancelled'
            and not self.message.metadata.get('interrupted')
        )

        # Themeable styling via QSS
        self.setObjectName("message_widget")
        self.setProperty("role", "user" if is_user else "assistant")
        self.setProperty("embedded", self.embedded)
        if not self.embedded and (self._is_editable_user_message() or self._can_delete_message()):
            self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self.customContextMenuRequested.connect(self._show_message_menu)

        # Never consume vertical slack from the scroll area; blank space belongs to the viewport.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(48 if is_user and not self.embedded else 0, 0, 0, 0)
        outer.setSpacing(2)
        self.message_body = QFrame()
        self.message_body.setObjectName("user_bubble" if is_user else "assistant_body")
        outer.addWidget(self.message_body)
        layout = QVBoxLayout(self.message_body)
        layout.setContentsMargins(16 if is_user else 0, 10 if is_user else 0, 16 if is_user else 0, 10 if is_user else 0)
        layout.setSpacing(6)

        if self.show_header and not is_user and not is_cancellation_notice:
            header = QHBoxLayout()
            header.setContentsMargins(0, 0, 0, 0)
            header.setSpacing(4)
            header.setAlignment(Qt.AlignmentFlag.AlignTop)

            if not self.embedded:
                avatar = QLabel()
                avatar.setPixmap(Icons.brand().pixmap(24, 24))
                header.addWidget(avatar)
            role_label = QLabel(QCoreApplication.translate('MessageWidget', '子助手') if self.embedded else "PyCat")
            role_label.setObjectName("message_role")
            role_label.setFixedHeight(MESSAGE_HEADER_HEIGHT)
            role_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            role_label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            header.addWidget(role_label)

            self._add_timestamp_badge(header)
            role_label.setToolTip(str(self.message.metadata.get("model_ref") or self.message.metadata.get("model") or ""))
            header.addStretch()
            layout.addLayout(header)

        # Thinking (assistant only) - show above final content
        if (not is_user) and self.show_thinking and self.message.thinking:
            self.thinking_widget = ThinkingSection(self.message.thinking)
            layout.addWidget(self.thinking_widget)

        # Content
        # MarkdownView handles str conversion
        if not is_cancellation_notice and (
            self.message.content or (not is_user and any(ref.mime.startswith('image/') for ref in self.delivery_refs))
        ):
            text, self._image_aliases, self._inline_images = message_image_markdown(
                str(self.message.content or ''), [*(self.message.content_refs or []), *self.delivery_refs],
                self.delivery_refs if not is_user else ())
            # Resolve mutable conversation state on the GUI thread; only the
            # immutable target, verification and decoding cross to the worker.
            loaders = {}
            if self.content_target_resolver is not None:
                for ref in set(self._image_aliases.values()):
                    if image_key(ref) not in self._inline_images:
                        continue
                    try:
                        target = self.content_target_resolver(ref, verify_integrity=False)
                        if target.is_image:
                            loaders[image_key(ref)] = partial(_read_message_image, target)
                    except Exception as exc:
                        logger.debug('Markdown image unavailable: %s', exc)
            content_view = MarkdownView(text, image_resources={source: loaders[image_key(ref)]
                for source, ref in self._image_aliases.items() if image_key(ref) in loaders})
            content_view.setOpenExternalLinks(False)
            content_view.setOpenLinks(False)
            content_view.anchorClicked.connect(self._open_markdown_link)
            content_view.image_clicked.connect(self._open_markdown_image)
            content_view.image_menu_requested.connect(self._show_markdown_image_menu)
            self._inline_images.intersection_update(loaders)
            content_view.setObjectName("message_content")
            content_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            if (not is_user and self.message.metadata.get("runtime_error")
                    and self.message.metadata.get("run_status") not in {"cancelled", "interrupted"}):
                error_panel = QFrame()
                error_panel.setObjectName("message_error_panel")
                error_panel.setAccessibleName(QCoreApplication.translate('MessageWidget', '模型调用失败'))
                error_layout = QVBoxLayout(error_panel)
                error_layout.setContentsMargins(12, 10, 12, 10)
                error_layout.setSpacing(6)
                error_label = QLabel(QCoreApplication.translate('MessageWidget', '调用失败'))
                error_label.setObjectName("message_error_label")
                error_layout.addWidget(error_label)
                error_layout.addWidget(content_view)
                layout.addWidget(error_panel)
            else:
                layout.addWidget(content_view)

        if self.message.content_refs:
            self._add_content_refs(layout)

        if not is_user and self.message.metadata.get("interrupted"):
            self._add_interruption_notice(layout)

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

        if is_cancellation_notice:
            status = SingleLineLabel(QCoreApplication.translate('MessageWidget', '已停止'))
            status.setObjectName('run_status_hint')
            status.setContentsMargins(0, 8, 0, 8)
            layout.addWidget(status)

        if not self.embedded:
            self.actions_widget = QWidget(self)
            actions = QHBoxLayout(self.actions_widget)
            actions.setContentsMargins(0, 0, 0, 0)
            actions.setSpacing(2)
            self._add_action_buttons(actions)
            self.actions_widget.setFixedHeight(MESSAGE_ACTION_SIZE)
            outer.addWidget(self.actions_widget, 0, Qt.AlignmentFlag.AlignRight if is_user else Qt.AlignmentFlag.AlignLeft)
            for button in self.actions_widget.findChildren(QToolButton):
                button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
                button.installEventFilter(self)

    def _sync_action_visibility(self):
        if not hasattr(self, "actions_widget"):
            return
        focus = QApplication.focusWidget()
        active = self.underMouse() or bool(focus and self.isAncestorOf(focus))
        for button in self.actions_widget.findChildren(MessageActionButton):
            button.revealed = active
            button.update()

    def enterEvent(self, event):
        super().enterEvent(event)
        self._sync_action_visibility()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._sync_action_visibility()

    def eventFilter(self, watched, event):
        if event.type() in {QEvent.Type.FocusIn, QEvent.Type.FocusOut}:
            self._focus_timer.start(0)
        return super().eventFilter(watched, event)

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

    def _add_interruption_notice(self, layout) -> None:
        metadata = self.message.metadata
        reason = str(metadata.get("interrupt_reason") or "")
        if reason == "output_limit":
            if not str(self.message.content or "").strip():
                text = QCoreApplication.translate('MessageWidget', '模型达到输出上限，未生成正文。请调整输出上限、推理强度或模型后继续。')
            else:
                text = QCoreApplication.translate('MessageWidget', '模型达到输出上限，回答尚未完成。可以继续，或先调整模型设置。')
        elif reason == "explicit_completion_missing":
            text = QCoreApplication.translate('MessageWidget', '尚未收到完成确认，运行已暂停。可以继续完成剩余步骤。')
        else:
            text = QCoreApplication.translate('MessageWidget', '运行尚未完成，可以继续。')
        notice = ThemedSelectableLabel(text)
        notice.setObjectName("message_interruption_notice")
        notice.setWordWrap(True)
        notice.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        details = [QCoreApplication.translate('MessageWidget', '此前工具结果和任务状态已保留。')]
        request_usage = metadata.get("request_usage")
        output_limit = request_usage.get("output_limit") if isinstance(request_usage, dict) else None
        if isinstance(output_limit, int) and output_limit > 0:
            details.append(QCoreApplication.translate('MessageWidget', '本次请求上限：{count} token').format(count=f"{output_limit:,}"))
        completion = metadata.get("completion_tokens")
        reasoning = metadata.get("reasoning_tokens")
        if isinstance(completion, int) and completion >= 0:
            details.append(QCoreApplication.translate('MessageWidget', '输出：{count} token').format(count=f"{completion:,}"))
        if isinstance(reasoning, int) and reasoning >= 0:
            details.append(QCoreApplication.translate('MessageWidget', '其中推理：{count} token').format(count=f"{reasoning:,}"))
        if (reason == "output_limit" and isinstance(output_limit, int)
                and isinstance(completion, int) and 0 < completion < output_limit):
            details.append(QCoreApplication.translate('MessageWidget', '服务端可能应用了更低的输出或推理上限。'))
        notice.setToolTip("\n".join(details))
        layout.addWidget(notice)

    def _add_timestamp_badge(self, layout):
        try:
            ts = message_time(self.message.created_at)
            label = self._add_badge(layout, ts)
            label.setToolTip(message_time(self.message.created_at, full=True))
        except Exception as exc:
            logger.debug("Failed to format message timestamp badge: %s", exc)

    def _add_badge(self, layout, text):
        label = QLabel(text)
        label.setObjectName("message_badge")
        self._style_header_badge(label)
        layout.addWidget(label)
        return label

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
            continue_btn = MessageActionButton(persistent=True)
            continue_btn.setIcon(Icons.get(Icons.PLAY))
            continue_btn.setIconSize(QSize(18, 18))
            continue_btn.setToolTip(QCoreApplication.translate('MessageWidget', '继续未完成的 Agent 运行'))
            continue_btn.setFixedSize(MESSAGE_ACTION_SIZE, MESSAGE_ACTION_SIZE)
            continue_btn.setObjectName("msg_continue_btn")
            continue_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            continue_btn.clicked.connect(lambda: self.continue_requested.emit(self.message.id))
            layout.addWidget(continue_btn)

        copy_btn = MessageActionButton()
        copy_btn.setIcon(Icons.get_muted(Icons.COPY))
        copy_btn.setIconSize(QSize(18, 18))
        copy_btn.setToolTip(QCoreApplication.translate('MessageWidget', '复制原文'))
        copy_btn.setFixedSize(MESSAGE_ACTION_SIZE, MESSAGE_ACTION_SIZE)
        copy_btn.setObjectName("msg_copy_btn")
        copy_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        copy_btn.clicked.connect(self._copy_original_content)
        self._copy_btn = copy_btn
        layout.addWidget(copy_btn)

        if self._is_editable_user_message():
            edit_btn = MessageActionButton()
            edit_btn.setIcon(Icons.get_muted(Icons.EDIT))
            edit_btn.setIconSize(QSize(18, 18))
            edit_btn.setToolTip(QCoreApplication.translate('MessageWidget', '编辑并重试；发送后删除该轮原回复和后续对话'))
            edit_btn.setFixedSize(MESSAGE_ACTION_SIZE, MESSAGE_ACTION_SIZE)
            edit_btn.setObjectName("msg_edit_btn")
            edit_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            edit_btn.clicked.connect(lambda: self.edit_requested.emit(self.message.id))
            edit_btn.setEnabled(self._revision_enabled)
            self._edit_btn = edit_btn
            layout.addWidget(edit_btn)
        elif self.message.role == "assistant" and self.allow_restart:
            regenerate_btn = MessageActionButton()
            regenerate_btn.setIcon(Icons.get_muted(Icons.REFRESH))
            regenerate_btn.setIconSize(QSize(18, 18))
            regenerate_btn.setToolTip(QCoreApplication.translate('MessageWidget', '重新生成；删除该轮回复和后续对话，已执行的操作不会撤销'))
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
            reuse_action = QAction(QCoreApplication.translate('MessageWidget', '复用到输入框（不修改历史）'), menu)
            reuse_action.triggered.connect(lambda: self.reuse_requested.emit(self.message.id))
            menu.addAction(reuse_action)
        if self._can_delete_message():
            if menu.actions():
                menu.addSeparator()
            delete_action = QAction(QCoreApplication.translate('MessageWidget', '删除该轮及后续对话'), menu)
            delete_action.setToolTip(QCoreApplication.translate('MessageWidget', '只修改 PyCat 记录，不撤销已经执行的操作'))
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
            if image_key(ref) not in self._inline_images:
                layout.addWidget(self._content_ref_widget(ref, context_label="输入"))

    def _add_delivery_refs(self, layout) -> None:
        remaining = [ref for ref in self.delivery_refs if image_key(ref) not in self._inline_images]
        if not remaining:
            return
        self.delivery_group = DeliveryGroup(
            remaining,
            lambda ref: self._content_ref_widget(ref, context_label="交付", compact_image=True),
            parent=self,
        )
        layout.addWidget(self.delivery_group)

    @pyqtSlot(str)
    def _open_markdown_image(self, source):
        action = self._markdown_image_action(source)
        if action is not None:
            action.open_default()

    @pyqtSlot(QUrl)
    def _open_markdown_link(self, url):
        if url.scheme().lower() == "workspace":
            path = url.path(QUrl.ComponentFormattingOption.FullyDecoded)
            if url.authority() or not path:
                return
            # Retain the delivered version's digest and owner. A path with no
            # registered ref is an explicit user selection, bounded by the
            # same workspace resolver as the Files pane, never an auto-read.
            refs = [*(self.message.content_refs or []), *self.delivery_refs]
            ref = next((ref for ref in reversed(refs) if ref.ref == f"workspace:{path}"), None)
            show_content(self, **({"ref": ref} if ref else {"workspace_path": path}))
        elif not url.scheme() and not url.path() and url.hasFragment():
            view = self.sender()
            if isinstance(view, MarkdownView):
                view.scrollToAnchor(url.fragment())
        else:
            QDesktopServices.openUrl(url)

    @pyqtSlot(str, QPoint)
    def _show_markdown_image_menu(self, source, position):
        action = self._markdown_image_action(source)
        if action is not None:
            action.create_context_menu().exec(position)

    def _markdown_image_action(self, source):
        ref = self._image_aliases.get(image_source(source))
        if ref is None:
            return
        key = image_key(ref)
        if key not in self._image_actions:
            widget = self._content_ref_widget(ref, context_label="交付")
            widget.hide()
            self._image_actions[key] = widget
        return self._image_actions[key]

    def _content_ref_widget(self, ref: ContentRef, *, context_label: str, compact_image: bool = False) -> ContentRefWidget:
        def resolve_target(
            item: ContentRef,
            *,
            verify_integrity: bool,
        ):
            if self.content_target_resolver is None:
                raise FileNotFoundError(str(item.ref or item.name or "content"))
            return self.content_target_resolver(
                item,
                verify_integrity=verify_integrity,
            )

        widget = ContentRefWidget(
            ref,
            resolve_target=resolve_target,
            context_label=context_label,
            compact_image=compact_image,
            allow_image_edit=True,
            parent=self,
        )
        widget.image_edit_requested.connect(self.image_edit_requested.emit)
        return widget

    def _copy_original_content(self) -> None:
        text = str(self.message.content or "")
        QGuiApplication.clipboard().setText(text)

        if not hasattr(self, "_copy_btn"):
            return

        try:
            self._copy_btn.setToolTip(QCoreApplication.translate('MessageWidget', '已复制'))
            QTimer.singleShot(1200, self._restore_copy_btn_tooltip)
        except RuntimeError:
            pass

    def _restore_copy_btn_tooltip(self):
        try:
            self._copy_btn.setToolTip(QCoreApplication.translate('MessageWidget', '复制原文'))
        except RuntimeError:
            pass

    def _open_image_preview(self, image_data: str):
        show_content(self, image_source=image_data)
