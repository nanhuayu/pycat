"""Right inspector panel for conversation state and runtime signals."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QFrame,
    QToolButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QMenu,
)

from pycat.models.conversation import Conversation
from pycat.models.contracts.session_state import TodoStatus
from pycat.core.channel import channel_origin_from_message
from pycat.core.state.operations import get_active_todos
from pycat.gui.widgets.capsule import CapsuleLabel, CapsuleRow, SingleLineLabel
from pycat.gui.utils.theme import INSPECTOR_MARGIN, configure_icon_button, prepare_context_menu
from pycat.gui.widgets.collapsible_section import CollapsibleSection
from pycat.gui.widgets.themed_line_edit import ThemedLineEdit
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.widgets.materials_panel import MaterialsPanel
from pycat.gui.widgets.memory_panel import MemoryPanel
from pycat.gui.dialogs.content_preview import show_content


class _TwoLineElideLabel(QLabel):
    """Label that keeps channel detail text to explicit lines without wrapping."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full_lines: list[str] = []
        self.setWordWrap(False)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.set_full_text(text)

    def set_full_text(self, text: str) -> None:
        lines = [line.strip() for line in str(text or "-").splitlines() if line.strip()]
        self._full_lines = lines[:2] or ["-"]
        self.setToolTip("\n".join(self._full_lines))
        self._refresh_text()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._refresh_text()

    def _refresh_text(self) -> None:
        width = max(24, self.contentsRect().width())
        metrics = self.fontMetrics()
        text = "\n".join(
            metrics.elidedText(line, Qt.TextElideMode.ElideMiddle, width)
            for line in self._full_lines
        )
        if self.text() != text:
            super().setText(text)


def _soft_wrap_reference(value: object) -> str:
    text = str(value or "")
    if len(text) <= 48:
        return text
    return "".join(f"{char}\u200b" if char in "/\\:?#&=_-." else char for char in text)


def _format_process_elapsed(seconds: float) -> str:
    total = max(0, int(seconds or 0))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h{mins:02d}m"


def _format_last_output(modified_at) -> str:
    if not modified_at:
        return "无输出"
    delta = max(0, int(datetime.now().timestamp() - float(modified_at)))
    if delta < 5:
        return "刚刚"
    if delta < 60:
        return f"{delta}s 前"
    minutes = delta // 60
    if minutes < 60:
        return f"{minutes}min 前"
    return f"{minutes // 60}h 前"


class InspectorPanel(QWidget):
    """Panel displaying conversation state, workspace artifacts, and runtime metrics."""

    projection_changed = pyqtSignal(object)
    task_create_requested = pyqtSignal(str)
    task_complete_requested = pyqtSignal(str)
    task_delete_requested = pyqtSignal(str)
    process_stop_requested = pyqtSignal(str)
    process_open_requested = pyqtSignal(str)
    process_stop_all_requested = pyqtSignal()
    processes_refresh_requested = pyqtSignal()

    _PROCESS_REFRESH_INTERVAL_MS = 2000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("inspector_panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumWidth(220)
        self.setMaximumWidth(420)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self._conversation: Optional[Conversation] = None
        self._app_state = None
        self._process_snapshots: list = []
        self._process_rows = {}
        self._mutations_enabled = True
        self._task_offset = 0
        self._setup_ui()
        self._process_timer = QTimer(self)
        self._process_timer.setInterval(self._PROCESS_REFRESH_INTERVAL_MS)
        self._process_timer.timeout.connect(self.processes_refresh_requested.emit)

    def showEvent(self, event):
        super().showEvent(event)
        self._process_timer.start()
        self.processes_refresh_requested.emit()

    def hideEvent(self, event):
        self._process_timer.stop()
        super().hideEvent(event)

    def _add_list_section(
        self,
        parent_layout: QVBoxLayout,
        title: str,
        *,
        summary: str,
        collapsed: bool,
        object_name: str,
        spacing: int = 4,
        visible: bool = True,
    ) -> tuple[CollapsibleSection, QFrame, QVBoxLayout]:
        """Create a collapsible section hosting a scrollable item list."""
        section = CollapsibleSection(title, summary=summary, collapsed=collapsed)
        section.body_layout.setContentsMargins(0, 0, 0, 4)
        section.body_layout.setSpacing(4)
        section.header.layout().setContentsMargins(0, 0, 0, 0)
        section.header.layout().setSpacing(4)
        section.toggle_btn.setFixedSize(28, 28)
        section.toggle_btn.setAccessibleName(title)
        parent_layout.addWidget(section)
        container = QFrame()
        container.setObjectName(f"{object_name}_container")
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(spacing)
        section.body_layout.addWidget(container)
        section.setVisible(visible)
        return section, container, container_layout

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("inspector_tabs")
        self.tabs.setAccessibleName("辅助栏")
        root.addWidget(self.tabs)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("inspector_scroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tabs.addTab(self.scroll, "任务")
        self.materials = MaterialsPanel()
        self.memory = MemoryPanel()
        self.tabs.addTab(self.materials, "资料")
        self.tabs.addTab(self.memory, "记忆")

        content = QWidget()
        content.setObjectName("inspector_scroll_content")
        content.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.scroll.setWidget(content)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(INSPECTOR_MARGIN, INSPECTOR_MARGIN, INSPECTOR_MARGIN, INSPECTOR_MARGIN)
        layout.setSpacing(4)

        add_row = QHBoxLayout()
        add_row.setSpacing(4)

        self.task_input_edit = ThemedLineEdit()
        self.task_input_edit.setObjectName("task_input_edit")
        self.task_input_edit.setPlaceholderText("新增任务…")
        self.task_input_edit.returnPressed.connect(self._emit_create_task)
        add_row.addWidget(self.task_input_edit, 1)

        self.add_task_btn = QToolButton()
        self.add_task_btn.setObjectName("add_task_btn")
        configure_icon_button(self.add_task_btn, Icons.get_muted(Icons.PLUS), "新增任务")
        self.add_task_btn.clicked.connect(self._emit_create_task)
        add_row.addWidget(self.add_task_btn)

        layout.addLayout(add_row)
        self.tasks_container = QFrame()
        self.tasks_container.setObjectName("tasks_container")
        self.tasks_layout = QVBoxLayout(self.tasks_container)
        self.tasks_layout.setContentsMargins(0, 0, 0, 0)
        self.tasks_layout.setSpacing(4)
        layout.addWidget(self.tasks_container)

        (self.processes_section, self.processes_container, self.processes_layout) = self._add_list_section(
            layout, "Shell 进程", summary="无后台进程", collapsed=True, object_name="processes",
        )

        self.stop_all_processes_btn = QToolButton()
        self.stop_all_processes_btn.setObjectName("stop_all_processes_btn")
        configure_icon_button(self.stop_all_processes_btn, Icons.get_muted(Icons.STOP), "全部停止")
        self.stop_all_processes_btn.clicked.connect(self.process_stop_all_requested.emit)
        self.processes_section.header.layout().addWidget(self.stop_all_processes_btn)

        (self.completed_tasks_section, self.completed_tasks_container, self.completed_tasks_layout) = self._add_list_section(
            layout, "最近完成", summary="暂无已完成任务", collapsed=True,
            object_name="completed_tasks", spacing=4, visible=False,
        )

        (self.channels_section, self.channels_container, self.channels_layout) = self._add_list_section(
            layout, "通道", summary="外部来源", collapsed=True,
            object_name="channels", visible=False,
        )

        layout.removeWidget(self.completed_tasks_section)
        layout.insertWidget(layout.indexOf(self.tasks_container) + 1, self.completed_tasks_section)
        self.processes_section.setVisible(False)
        layout.addStretch(1)

        self._render_tasks(None)
        self._render_channels(None)
        self._set_task_controls_enabled(False)

    def _set_task_controls_enabled(self, enabled: bool) -> None:
        available = bool(enabled) and self._mutations_enabled
        self.task_input_edit.setEnabled(available)
        self.add_task_btn.setEnabled(available)

    def set_mutations_enabled(self, enabled: bool) -> None:
        self._mutations_enabled = bool(enabled)
        self._set_task_controls_enabled(bool(self._conversation))

    def _emit_create_task(self) -> None:
        if not self._conversation or not self._mutations_enabled:
            return
        text = (self.task_input_edit.text() or "").strip()
        if not text:
            return
        self.task_input_edit.setText("")
        self.task_create_requested.emit(text)

    def _render_tasks(self, conversation: Optional[Conversation]) -> None:
        self._clear_layout(self.tasks_layout)
        self._clear_layout(self.completed_tasks_layout)

        if not conversation:
            self.completed_tasks_section.setVisible(False)
            empty = QLabel("选择会话后查看任务")
            empty.setProperty("muted", True)
            self.tasks_layout.addWidget(empty)
            return

        try:
            state = conversation.get_state()
            active_tasks = list(get_active_todos(state) or [])
            recent_tasks = list(getattr(state, "recent_completed_todos", []) or [])
        except Exception:
            active_tasks = []
            recent_tasks = []

        if not active_tasks:
            empty = QLabel("暂无任务")
            empty.setProperty("muted", True)
            self.tasks_layout.addWidget(empty)
        else:
            start = min(self._task_offset, max(0, ((len(active_tasks) - 1) // 8) * 8))
            for task in active_tasks[start:start + 8]:
                self.tasks_layout.addWidget(self._create_task_row(task, actions=True))
            if len(active_tasks) > 8:
                more = QToolButton()
                more.setText(f"下一页 · {start // 8 + 1}/{(len(active_tasks) + 7) // 8}")
                more.setAccessibleName("浏览下一页任务")
                def next_tasks():
                    self._task_offset = 0 if start + 8 >= len(active_tasks) else start + 8
                    self._render_tasks(self._conversation)
                more.clicked.connect(next_tasks)
                self.tasks_layout.addWidget(more)

        completed = list(reversed(recent_tasks[-4:]))
        self.completed_tasks_section.setVisible(bool(completed))
        self.completed_tasks_section.set_title(
            f"最近完成 ({len(completed)})" if completed else "最近完成"
        )
        self.completed_tasks_section.set_summary("最近里程碑" if completed else "暂无已完成任务")
        for task in completed:
            self.completed_tasks_layout.addWidget(self._create_task_row(task, actions=False))

    def _create_task_row(self, task, *, actions: bool) -> QFrame:
        row = CapsuleRow()
        row.setObjectName("task_card")
        row_layout = row.layout()

        status = self._todo_status(getattr(task, "status", TodoStatus.PENDING))
        status_label = self._todo_status_label(status)
        title = str(getattr(task, "title", "") or "").strip() or "未命名任务"
        icon = QLabel()
        icon.setObjectName("task_status_icon")
        icon.setProperty("status", status.value)
        icon.setFixedSize(16, 16)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setToolTip(status_label)
        icon.setPixmap(self._todo_status_icon(status).pixmap(16, 16))
        row_layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignVCenter)

        label = CapsuleLabel(title)
        label.setObjectName("task_text")
        label.setWordWrap(False)
        label.setMinimumWidth(0)
        tooltip_parts = [f"状态：{status_label}", title]
        description = str(getattr(task, "note", "") or "").strip()
        blocked_reason = str(getattr(task, "blocked_reason", "") or "").strip()
        if description:
            tooltip_parts.append(description)
        if blocked_reason:
            tooltip_parts.append(f"阻塞：{blocked_reason}")
        label.setToolTip("\n".join(tooltip_parts))
        label.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        row_layout.addWidget(label, 1)

        task_id = str(getattr(task, "id", "") or "").strip()
        refs = tuple(getattr(task, "refs", None) or ())
        conversation = self._conversation
        def mutate(signal):
            if self._conversation is conversation and self._mutations_enabled:
                signal.emit(task_id)
        if (actions and task_id) or refs:
            more = QToolButton()
            more.setObjectName("task_more_btn")
            configure_icon_button(more, Icons.get_muted(Icons.MORE), "任务操作")
            menu = prepare_context_menu(QMenu(more), self)
            def populate():
                prepare_context_menu(menu, self)
                menu.clear()
                for ref in refs:
                    menu.addAction(f"查看来源：{ref}", lambda _checked=False, value=ref: show_content(self, ref=value))
                if actions and task_id:
                    if refs:
                        menu.addSeparator()
                    complete = menu.addAction(Icons.get_success(Icons.CHECK), "标记完成", lambda: mutate(self.task_complete_requested))
                    complete.setEnabled(self._mutations_enabled)
                    delete = menu.addAction("删除任务", lambda: mutate(self.task_delete_requested))
                    delete.setEnabled(self._mutations_enabled)
            menu.aboutToShow.connect(populate)
            more.setMenu(menu)
            more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            row_layout.addWidget(more)
        return row

    @staticmethod
    def _todo_status(value) -> TodoStatus:
        if isinstance(value, TodoStatus):
            return value
        raw = str(value or TodoStatus.PENDING.value).strip()
        return TodoStatus(raw) if raw in {item.value for item in TodoStatus} else TodoStatus.PENDING

    @staticmethod
    def _todo_status_label(status: TodoStatus) -> str:
        return {
            TodoStatus.IN_PROGRESS: "进行中",
            TodoStatus.PENDING: "待办",
            TodoStatus.BLOCKED: "阻塞",
            TodoStatus.COMPLETED: "已完成",
            TodoStatus.CANCELLED: "已取消",
        }.get(status, "待办")

    @staticmethod
    def _todo_status_icon(status: TodoStatus):
        if status == TodoStatus.IN_PROGRESS:
            return Icons.get(Icons.PLAY)
        if status == TodoStatus.BLOCKED:
            return Icons.get_warning(Icons.CIRCLE_INFO)
        if status == TodoStatus.COMPLETED:
            return Icons.get_success(Icons.CIRCLE_CHECK)
        if status == TodoStatus.CANCELLED:
            return Icons.get_muted(Icons.CIRCLE_XMARK)
        return Icons.get_muted(Icons.CIRCLE_INFO)

    def _clear_layout(self, layout: QVBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.deleteLater()

    def _render_channels(self, conversation: Optional[Conversation]) -> None:
        self._clear_layout(self.channels_layout)
        if not conversation:
            self.channels_section.setVisible(False)
            return

        origins = []
        seen = set()
        for message in reversed(getattr(conversation, "messages", []) or []):
            origin = channel_origin_from_message(message)
            if origin is None:
                continue
            key = (origin.source, origin.thread_id, origin.user)
            if key in seen:
                continue
            seen.add(key)
            origins.append(origin)
            if len(origins) >= 4:
                break

        origins = list(reversed(origins))
        if not origins:
            self.channels_section.setVisible(False)
            return

        self.channels_section.setVisible(True)
        self.channels_section.set_title(f"外部来源 ({len(origins)})")
        self.channels_section.set_summary("Channel 会话")
        for origin in origins:
            details = [value for value in (origin.thread_id, origin.message_id) if value]
            self._add_channel_row(
                origin.display_name,
                "\n".join(details) if details else origin.source,
            )

    def update_processes(self, snapshots: list) -> None:
        """Replace the Shell process section with a read-only snapshot list."""
        was_empty = not self._process_snapshots
        self._process_snapshots = list(snapshots or [])
        count = len(self._process_snapshots)
        self.processes_section.setVisible(count > 0)
        self.processes_section.set_title("Shell 进程" if count == 0 else f"Shell 进程 ({count})")
        self.processes_section.set_summary("")
        if was_empty or count == 0:
            self.processes_section.set_collapsed(count == 0)
        self.stop_all_processes_btn.setVisible(any(s.running for s in self._process_snapshots))
        live_ids = {s.process_id for s in self._process_snapshots}
        for pid in tuple(self._process_rows):
            if pid not in live_ids:
                row = self._process_rows.pop(pid)
                self.processes_layout.removeWidget(row)
                row.deleteLater()
        for snapshot in self._process_snapshots:
            row = self._process_rows.get(snapshot.process_id)
            if row is None:
                row = self._create_process_row(snapshot)
                self._process_rows[snapshot.process_id] = row
                self.processes_layout.addWidget(row)
            status = "运行中" if snapshot.running else f"已退出({snapshot.exit_code})"
            row.findChild(QLabel, "process_meta").setText(
                f"pid={snapshot.pid} · {status} · {_format_process_elapsed(snapshot.elapsed_sec)}")
            row.findChild(QToolButton, "process_stop_btn").setEnabled(snapshot.running)

    def _create_process_row(self, snapshot) -> QFrame:
        row = QFrame()
        row.setObjectName("process_row")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(4, 2, 0, 2)
        row_layout.setSpacing(4)

        text_column = QVBoxLayout()
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(0)

        command = str(getattr(snapshot, "command", "") or "-")
        command_label = _TwoLineElideLabel(command)
        command_label.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        command_label.setToolTip(
            f"{command}\nprocess_id={getattr(snapshot, 'process_id', '')}\nlog={getattr(snapshot, 'log_path', '')}"
        )
        text_column.addWidget(command_label)

        status = "运行中" if bool(getattr(snapshot, "running", False)) else f"已退出({getattr(snapshot, 'exit_code', '')})"
        meta = (
            f"pid={int(getattr(snapshot, 'pid', 0) or 0)} · {status} · "
            f"{_format_process_elapsed(float(getattr(snapshot, 'elapsed_sec', 0.0) or 0.0))} · "
            f"输出 {_format_last_output(getattr(snapshot, 'last_output_at', None))}"
        )
        meta_label = QLabel(meta)
        meta_label.setObjectName("process_meta")
        meta_label.setProperty("muted", True)
        meta_label.setMinimumWidth(0)
        meta_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        meta_label.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        text_column.addWidget(meta_label)
        row_layout.addLayout(text_column, 1)

        open_btn = QToolButton()
        open_btn.setObjectName("process_open_btn")
        configure_icon_button(open_btn, Icons.get_muted(Icons.TERMINAL), "查看 Shell")
        open_btn.clicked.connect(lambda _checked=False, pid=snapshot.process_id: self.process_open_requested.emit(pid))
        row_layout.addWidget(open_btn, 0, Qt.AlignmentFlag.AlignTop)

        stop_btn = QToolButton()
        stop_btn.setObjectName("process_stop_btn")
        configure_icon_button(stop_btn, Icons.get_muted(Icons.STOP), "停止该进程")
        process_id = str(getattr(snapshot, "process_id", "") or "")
        stop_btn.clicked.connect(lambda _checked=False, pid=process_id: self.process_stop_requested.emit(pid))
        row_layout.addWidget(stop_btn, 0, Qt.AlignmentFlag.AlignTop)
        return row

    def update_stats(self, conversation: Optional[Conversation]):
        if getattr(conversation, "id", "") != getattr(self._conversation, "id", ""):
            self._task_offset = 0
        self._conversation = conversation
        self._set_task_controls_enabled(bool(conversation))
        self._render_tasks(conversation)
        self.projection_changed.emit(conversation)
        self._render_channels(conversation)
        if not conversation:
            return

    def update_conversation_state(
        self,
        conversation: Optional[Conversation],
        changed_fields: set[str] | frozenset[str],
    ) -> None:
        self._conversation = conversation
        self._set_task_controls_enabled(bool(conversation))
        fields = set(changed_fields or set())
        if not conversation:
            self.update_stats(None)
            return
        if fields & {"todos", "recent_completed_todos"}:
            self._render_tasks(conversation)
        if fields & {"memory", "artifacts", "messages", "wiki"}:
            self.projection_changed.emit(conversation)
        if "messages" in fields:
            self._render_channels(conversation)

    def update_app_state(self, app_state) -> None:
        previous_enabled = (
            bool(getattr(self._app_state, "memory_enabled", True))
            if self._app_state is not None
            else True
        )
        next_enabled = bool(getattr(app_state, "memory_enabled", True))
        previous_revision = int(getattr(self._app_state, "content_revision", 0))
        self._app_state = app_state
        if previous_enabled != next_enabled or previous_revision != int(getattr(app_state, "content_revision", 0)):
            self.projection_changed.emit(self._conversation)

    def _add_channel_row(self, title_text: str, detail_text: str) -> None:
        row = QFrame()
        row.setObjectName("channel_origin_row")
        row.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        row_layout = QVBoxLayout(row)
        row_layout.setContentsMargins(0, 2, 0, 2)
        row_layout.setSpacing(1)

        title = SingleLineLabel(self._channel_row_title(str(title_text or "通道")))
        title.setObjectName("channel_origin_title")
        title.setWordWrap(False)
        title.setMinimumWidth(0)
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        title.setToolTip(str(title_text or "通道"))
        row_layout.addWidget(title)

        detail_label = _TwoLineElideLabel(str(detail_text or "-"))
        detail_label.setObjectName("channel_origin_detail")
        detail_label.setProperty("muted", True)
        detail_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        detail_label.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        row_layout.addWidget(detail_label)

        self.channels_layout.addWidget(row)

    @staticmethod
    def _channel_row_title(text: str) -> str:
        value = str(text or "").strip() or "通道"
        if " / " in value:
            value = value.split(" / ", 1)[0].strip() or value
        if ":" in value:
            value = value.rsplit(":", 1)[-1].strip() or value
        return value
