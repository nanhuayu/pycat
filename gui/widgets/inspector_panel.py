"""Right inspector panel for conversation state and runtime signals."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QFrame,
    QToolButton,
    QScrollArea,
    QSizePolicy,
)

from models.conversation import Conversation
from models.contracts.content import FileChange
from models.contracts.session_state import TodoStatus
from core.channel import channel_origin_from_message
from core.content.resolver import SessionContentResolver
from core.state.artifact import ArtifactService
from gui.widgets.collapsible_section import CollapsibleSection
from gui.widgets.themed_line_edit import ThemedLineEdit, ThemedSelectableLabel
from gui.widgets.workflow_capsule import (
    WorkflowCapsuleRow,
    create_artifact_capsule,
)
from core.content.references import delivery_refs_for_messages
from gui.utils.icon_manager import Icons


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


class RuntimeStrip(QFrame):
    """Compact current-run status projection."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("runtime_strip")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        self.title = QLabel("空闲")
        self.title.setObjectName("runtime_title")
        layout.addWidget(self.title)

        self.detail = QLabel("-")
        self.detail.setObjectName("runtime_detail")
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)

    def set_state(self, title: str, detail: str = "-") -> None:
        self.title.setText(str(title or "空闲"))
        self.detail.setText(str(detail or "-"))


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

    artifact_open_requested = pyqtSignal(str)
    task_create_requested = pyqtSignal(str)
    task_complete_requested = pyqtSignal(str)
    task_delete_requested = pyqtSignal(str)
    memory_candidate_promote_requested = pyqtSignal(str)
    memory_candidate_reject_requested = pyqtSignal(str)
    process_stop_requested = pyqtSignal(str)
    process_stop_all_requested = pyqtSignal()
    processes_refresh_requested = pyqtSignal()

    _PROCESS_REFRESH_INTERVAL_MS = 2000

    def __init__(self, parent=None, *, content_service=None):
        super().__init__(parent)
        self.setObjectName("inspector_panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumWidth(220)
        self.setMaximumWidth(420)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self._conversation: Optional[Conversation] = None
        self._app_state = None
        self._process_snapshots: list = []
        self._mutations_enabled = True
        self._workspace_memory_key = ""
        self._workspace_memory_count: Optional[int] = None
        self._content_service = content_service
        self._content_resolver = SessionContentResolver(content_service) if content_service is not None else None
        self._setup_ui()
        self._process_timer = QTimer(self)
        self._process_timer.setInterval(self._PROCESS_REFRESH_INTERVAL_MS)
        self._process_timer.timeout.connect(self.processes_refresh_requested.emit)
        self._process_timer.start()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("inspector_scroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(self.scroll)

        content = QWidget()
        content.setObjectName("inspector_scroll_content")
        content.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.scroll.setWidget(content)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        self.tasks_section = CollapsibleSection("任务", summary="当前任务与待办", collapsed=False)
        layout.addWidget(self.tasks_section)

        add_row = QHBoxLayout()
        add_row.setSpacing(6)

        self.task_input_edit = ThemedLineEdit()
        self.task_input_edit.setObjectName("task_input_edit")
        self.task_input_edit.setPlaceholderText("新增任务…")
        self.task_input_edit.returnPressed.connect(self._emit_create_task)
        add_row.addWidget(self.task_input_edit, 1)

        self.add_task_btn = QToolButton()
        self.add_task_btn.setObjectName("add_task_btn")
        self.add_task_btn.setIcon(Icons.get(Icons.PLUS))
        self.add_task_btn.setAutoRaise(True)
        self.add_task_btn.setFixedSize(22, 22)
        self.add_task_btn.setToolTip("新增任务")
        self.add_task_btn.clicked.connect(self._emit_create_task)
        add_row.addWidget(self.add_task_btn)

        self.tasks_section.body_layout.addLayout(add_row)

        self.tasks_container = QFrame()
        self.tasks_container.setObjectName("tasks_container")
        self.tasks_layout = QVBoxLayout(self.tasks_container)
        self.tasks_layout.setContentsMargins(0, 0, 0, 0)
        self.tasks_layout.setSpacing(1)
        self.tasks_section.body_layout.addWidget(self.tasks_container)

        self.documents_section = CollapsibleSection("内容", summary="暂无内容", collapsed=True)
        layout.addWidget(self.documents_section)

        self.documents_container = QFrame()
        self.documents_container.setObjectName("documents_container")
        self.documents_layout = QVBoxLayout(self.documents_container)
        self.documents_layout.setContentsMargins(0, 0, 0, 0)
        self.documents_layout.setSpacing(4)
        self.documents_section.body_layout.addWidget(self.documents_container)

        self.memory_section = CollapsibleSection("记忆", summary="会话 / 工作区", collapsed=True)
        layout.addWidget(self.memory_section)
        self.memory_container = QFrame()
        self.memory_container.setObjectName("memory_container")
        self.memory_layout = QVBoxLayout(self.memory_container)
        self.memory_layout.setContentsMargins(0, 0, 0, 0)
        self.memory_layout.setSpacing(4)
        self.memory_section.body_layout.addWidget(self.memory_container)

        self.processes_section = CollapsibleSection("Shell 进程", summary="无后台进程", collapsed=True)
        layout.addWidget(self.processes_section)

        processes_header = QHBoxLayout()
        processes_header.setSpacing(6)
        self.processes_count_label = QLabel("0 个进行中")
        self.processes_count_label.setProperty("muted", True)
        processes_header.addWidget(self.processes_count_label, 1)
        self.stop_all_processes_btn = QToolButton()
        self.stop_all_processes_btn.setObjectName("stop_all_processes_btn")
        self.stop_all_processes_btn.setIcon(Icons.get(Icons.STOP, scale_factor=0.85))
        self.stop_all_processes_btn.setAutoRaise(True)
        self.stop_all_processes_btn.setFixedSize(22, 22)
        self.stop_all_processes_btn.setToolTip("全部停止")
        self.stop_all_processes_btn.clicked.connect(self.process_stop_all_requested.emit)
        processes_header.addWidget(self.stop_all_processes_btn)
        self.processes_section.body_layout.addLayout(processes_header)

        self.processes_container = QFrame()
        self.processes_container.setObjectName("processes_container")
        self.processes_layout = QVBoxLayout(self.processes_container)
        self.processes_layout.setContentsMargins(0, 0, 0, 0)
        self.processes_layout.setSpacing(4)
        self.processes_section.body_layout.addWidget(self.processes_container)

        self.completed_tasks_section = CollapsibleSection("最近完成", summary="暂无已完成任务", collapsed=True)
        layout.addWidget(self.completed_tasks_section)
        self.completed_tasks_container = QFrame()
        self.completed_tasks_container.setObjectName("completed_tasks_container")
        self.completed_tasks_layout = QVBoxLayout(self.completed_tasks_container)
        self.completed_tasks_layout.setContentsMargins(0, 0, 0, 0)
        self.completed_tasks_layout.setSpacing(1)
        self.completed_tasks_section.body_layout.addWidget(self.completed_tasks_container)
        self.completed_tasks_section.setVisible(False)

        self.channels_section = CollapsibleSection("通道", summary="外部来源", collapsed=True)
        layout.addWidget(self.channels_section)

        self.channels_container = QFrame()
        self.channels_container.setObjectName("channels_container")
        self.channels_layout = QVBoxLayout(self.channels_container)
        self.channels_layout.setContentsMargins(0, 0, 0, 0)
        self.channels_layout.setSpacing(4)
        self.channels_section.body_layout.addWidget(self.channels_container)
        self.channels_section.setVisible(False)
        
        layout.addStretch(1)

        self._render_tasks(None)
        self._render_memory(None)
        self._render_artifacts(None)
        self._render_channels(None)
        self._set_task_controls_enabled(False)
        self.artifact_open_requested.connect(self._open_artifact_path)

    def _set_task_controls_enabled(self, enabled: bool) -> None:
        available = bool(enabled) and self._mutations_enabled
        self.task_input_edit.setEnabled(available)
        self.add_task_btn.setEnabled(available)

    def set_mutations_enabled(self, enabled: bool) -> None:
        self._mutations_enabled = bool(enabled)
        self._set_task_controls_enabled(bool(self._conversation))
        for object_name in (
            "task_done_btn",
            "task_delete_btn",
            "memory_promote_btn",
            "memory_reject_btn",
        ):
            for button in self.findChildren(QToolButton, object_name):
                button.setEnabled(self._mutations_enabled)

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
            self.tasks_section.set_title("当前任务")
            self.tasks_section.set_summary("暂无任务")
            self.completed_tasks_section.setVisible(False)
            empty = QLabel("-")
            empty.setProperty("muted", True)
            self.tasks_layout.addWidget(empty)
            return

        try:
            state = conversation.get_state()
            from core.state.operations import get_active_todos

            active_tasks = list(get_active_todos(state) or [])
            recent_tasks = list(getattr(state, "recent_completed_todos", []) or [])
        except Exception:
            active_tasks = []
            recent_tasks = []

        active_count = len(active_tasks)
        self.tasks_section.set_title(f"当前任务 ({active_count})" if active_count else "当前任务")
        in_progress = next(
            (task for task in active_tasks if self._todo_status(getattr(task, "status", None)) == TodoStatus.IN_PROGRESS),
            None,
        )
        blocked = next(
            (task for task in active_tasks if self._todo_status(getattr(task, "status", None)) == TodoStatus.BLOCKED),
            None,
        )
        if blocked is not None:
            self.tasks_section.set_summary("存在阻塞")
        elif in_progress is not None:
            self.tasks_section.set_summary("正在执行")
        elif active_count:
            self.tasks_section.set_summary("等待执行")
        else:
            self.tasks_section.set_summary("暂无任务")

        if not active_tasks:
            empty = QLabel("暂无任务")
            empty.setProperty("muted", True)
            self.tasks_layout.addWidget(empty)
        else:
            for task in active_tasks[:8]:
                self.tasks_layout.addWidget(self._create_task_row(task, actions=True))
            if len(active_tasks) > 8:
                more = QLabel(f"+{len(active_tasks) - 8} 个任务未显示")
                more.setProperty("muted", True)
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
        row = QFrame()
        row.setObjectName("task_card")
        row.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(5, 2, 4, 2)
        row_layout.setSpacing(5)

        status = self._todo_status(getattr(task, "status", TodoStatus.PENDING))
        status_label = self._todo_status_label(status)
        title = str(getattr(task, "title", "") or "").strip() or "未命名任务"
        icon = QLabel()
        icon.setObjectName("task_status_icon")
        icon.setProperty("status", status.value)
        icon.setFixedSize(16, 16)
        icon.setToolTip(status_label)
        icon.setPixmap(self._todo_status_icon(status).pixmap(16, 16))
        row_layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignVCenter)

        label = QLabel(title)
        label.setObjectName("task_text")
        label.setWordWrap(False)
        label.setMinimumWidth(0)
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        tooltip_parts = [f"状态：{status_label}", title]
        description = str(getattr(task, "description", "") or "").strip()
        blocked_reason = str(getattr(task, "blocked_reason", "") or "").strip()
        if description:
            tooltip_parts.append(description)
        if blocked_reason:
            tooltip_parts.append(f"阻塞：{blocked_reason}")
        label.setToolTip("\n".join(tooltip_parts))
        label.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        row_layout.addWidget(label, 1)

        task_id = str(getattr(task, "id", "") or "").strip()
        if actions and task_id:
            action_group = QWidget()
            action_group.setObjectName("task_action_group")
            action_layout = QHBoxLayout(action_group)
            action_layout.setContentsMargins(0, 0, 0, 0)
            action_layout.setSpacing(1)

            done_btn = QToolButton()
            done_btn.setObjectName("task_done_btn")
            done_btn.setIcon(Icons.get_success(Icons.CHECK))
            done_btn.setAutoRaise(True)
            done_btn.setFixedSize(22, 22)
            done_btn.setIconSize(QSize(15, 15))
            done_btn.setToolTip("标记完成")
            done_btn.setAccessibleName("标记完成")
            done_btn.setEnabled(self._mutations_enabled)
            done_btn.clicked.connect(
                lambda _checked=False, item_id=task_id: self.task_complete_requested.emit(item_id)
            )
            action_layout.addWidget(done_btn)

            delete_btn = QToolButton()
            delete_btn.setObjectName("task_delete_btn")
            delete_btn.setIcon(Icons.get_muted(Icons.XMARK))
            delete_btn.setAutoRaise(True)
            delete_btn.setFixedSize(22, 22)
            delete_btn.setIconSize(QSize(15, 15))
            delete_btn.setToolTip("删除")
            delete_btn.setAccessibleName("删除任务")
            delete_btn.setEnabled(self._mutations_enabled)
            delete_btn.clicked.connect(
                lambda _checked=False, item_id=task_id: self.task_delete_requested.emit(item_id)
            )
            action_layout.addWidget(delete_btn)
            row_layout.addWidget(action_group)
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
                w.deleteLater()

    def _render_memory(self, conversation: Optional[Conversation]) -> None:
        self._clear_layout(self.memory_layout)
        selected_sources = tuple(getattr(self._app_state, "selected_memory_sources", ("session", "workspace")) or ())
        session_enabled = "session" in selected_sources
        workspace_enabled = "workspace" in selected_sources

        source_label = ThemedSelectableLabel(f"来源：{self._memory_sources_text(selected_sources)}")
        source_label.setProperty("muted", True)
        source_label.setWordWrap(True)
        source_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.memory_layout.addWidget(source_label)

        if not conversation:
            empty = QLabel("-")
            empty.setProperty("muted", True)
            self.memory_layout.addWidget(empty)
            return

        try:
            state = conversation.get_state()
            memory = dict((state.memory or {}))
            candidates = [
                item
                for item in (state.memory_candidates or {}).values()
                if str(getattr(item, "status", "pending") or "pending") == "pending"
            ]
            workspace_count = self._get_workspace_memory_count(conversation)
        except Exception:
            memory = {}
            candidates = []
            workspace_count = 0

        visible_memory = dict(memory) if session_enabled else {}
        visible_workspace_count = workspace_count if workspace_enabled else 0

        total_count = len(visible_memory) + visible_workspace_count + len(candidates)
        self.memory_section.set_title(f"记忆 ({total_count})")
        self.memory_section.set_summary(
            f"{len(candidates)} 条待审核" if candidates else self._memory_sources_text(selected_sources)
        )

        if candidates:
            queue_label = QLabel("待审核候选")
            queue_label.setObjectName("task_text")
            self.memory_layout.addWidget(queue_label)
            for candidate in candidates[:5]:
                card = QFrame()
                card.setObjectName("task_card")
                card_layout = QVBoxLayout(card)
                card_layout.setContentsMargins(8, 6, 8, 6)
                card_layout.setSpacing(4)

                candidate_id = str(getattr(candidate, "id", "") or "")
                scope = str(getattr(candidate, "scope", "session") or "session")
                category = str(getattr(candidate, "category", "fact") or "fact")
                content = str(getattr(candidate, "content", "") or "")
                reason = str(getattr(candidate, "reason", "") or "")
                refs = list(getattr(candidate, "refs", []) or [])

                title_row = QHBoxLayout()
                title_row.setSpacing(4)
                title = _TwoLineElideLabel(f"{scope} · {category}")
                title.setProperty("muted", True)
                title_row.addWidget(title, 1)
                promote = QToolButton()
                promote.setObjectName("memory_promote_btn")
                promote.setIcon(Icons.get_success(Icons.CHECK))
                promote.setToolTip("提升为正式记忆")
                promote.setFixedSize(20, 20)
                promote.setEnabled(self._mutations_enabled)
                promote.clicked.connect(
                    lambda _checked=False, candidate_id=candidate_id: self.memory_candidate_promote_requested.emit(candidate_id)
                )
                title_row.addWidget(promote)
                reject = QToolButton()
                reject.setObjectName("memory_reject_btn")
                reject.setIcon(Icons.get_error(Icons.XMARK))
                reject.setToolTip("拒绝候选")
                reject.setFixedSize(20, 20)
                reject.setEnabled(self._mutations_enabled)
                reject.clicked.connect(
                    lambda _checked=False, candidate_id=candidate_id: self.memory_candidate_reject_requested.emit(candidate_id)
                )
                title_row.addWidget(reject)
                card_layout.addLayout(title_row)

                content_label = ThemedSelectableLabel(content or "-")
                content_label.setWordWrap(True)
                content_label.setMinimumWidth(0)
                content_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                content_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                tooltip = "\n".join(part for part in (reason, *refs) if part)
                if tooltip:
                    content_label.setToolTip(tooltip)
                card_layout.addWidget(content_label)
                if refs:
                    refs_label = ThemedSelectableLabel(
                        "引用：" + "，".join(_soft_wrap_reference(ref) for ref in refs[:3])
                    )
                    refs_label.setProperty("muted", True)
                    refs_label.setWordWrap(True)
                    refs_label.setMinimumWidth(0)
                    refs_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                    refs_label.setToolTip("\n".join(str(ref) for ref in refs[:3]))
                    card_layout.addWidget(refs_label)
                self.memory_layout.addWidget(card)

        if not selected_sources:
            empty = QLabel("当前会话已禁用记忆注入；候选审核仍可使用。")
            empty.setProperty("muted", True)
            empty.setWordWrap(True)
            self.memory_layout.addWidget(empty)
            return
        if visible_workspace_count > 0:
            workspace_card = QFrame()
            workspace_card.setObjectName("task_card")
            workspace_layout = QVBoxLayout(workspace_card)
            workspace_layout.setContentsMargins(10, 8, 10, 8)
            workspace_layout.setSpacing(4)

            workspace_title = QLabel("工作区记忆")
            workspace_title.setObjectName("task_text")
            workspace_layout.addWidget(workspace_title)

            workspace_detail = ThemedSelectableLabel(
                f"已发现 {visible_workspace_count} 条 `.pycat/memory` 记忆条目"
            )
            workspace_detail.setProperty("muted", True)
            workspace_detail.setWordWrap(True)
            workspace_detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            workspace_layout.addWidget(workspace_detail)
            self.memory_layout.addWidget(workspace_card)

        if not visible_memory:
            empty_text = "暂无记忆条目" if visible_workspace_count <= 0 else "当前未存储会话记忆，已使用工作区记忆。"
            empty = QLabel(empty_text)
            empty.setProperty("muted", True)
            self.memory_layout.addWidget(empty)
            return

        for key, value in list(visible_memory.items())[:5]:
            card = QFrame()
            card.setObjectName("task_card")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(10, 8, 10, 8)
            card_layout.setSpacing(4)

            key_label = QLabel(str(key))
            key_label.setObjectName("task_text")
            card_layout.addWidget(key_label)

            preview = str(getattr(value, "content", value) or "")
            if len(preview) > 100:
                preview = preview[:100] + "..."
            value_label = ThemedSelectableLabel(preview or "-")
            value_label.setWordWrap(True)
            value_label.setProperty("muted", True)
            value_label.setToolTip(str(value or "") or preview or "-")
            value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            card_layout.addWidget(value_label)

            self.memory_layout.addWidget(card)

    def _render_artifacts(self, conversation: Optional[Conversation]) -> None:
        self._clear_layout(self.documents_layout)

        if not conversation:
            self.documents_section.set_title("内容")
            self.documents_section.set_summary("暂无内容")
            empty = QLabel("-")
            empty.setProperty("muted", True)
            self.documents_layout.addWidget(empty)
            return

        inputs = self._input_refs(conversation)
        deliveries = delivery_refs_for_messages(getattr(conversation, "messages", []) or [])
        try:
            artifacts = dict((conversation.get_state().artifacts or {}))
        except Exception:
            artifacts = {}
        changes = self._file_changes(conversation)
        total = len(inputs) + len(deliveries) + len(artifacts) + len(changes)

        self.documents_section.set_title(f"内容 ({total})" if total else "内容")
        summary_parts = []
        if inputs:
            summary_parts.append(f"输入 {len(inputs)}")
        if deliveries:
            summary_parts.append(f"交付 {len(deliveries)}")
        if artifacts:
            summary_parts.append(f"产出 {len(artifacts)}")
        if changes:
            summary_parts.append(f"变更 {len(changes)}")
        self.documents_section.set_summary(" · ".join(summary_parts) or "暂无内容")
        self.documents_section.set_collapsed(not bool(total))
        if not total:
            empty = QLabel("暂无内容")
            empty.setProperty("muted", True)
            self.documents_layout.addWidget(empty)
            return

        for ref in inputs:
            self.documents_layout.addWidget(self._create_input_row(ref))
        for ref in deliveries:
            self.documents_layout.addWidget(self._create_delivery_row(ref))
        for name, doc in artifacts.items():
            self.documents_layout.addWidget(self._create_artifact_row(str(name), doc))
        for change in changes:
            self.documents_layout.addWidget(self._create_file_change_row(change))

    @staticmethod
    def _input_refs(conversation: Conversation) -> list:
        refs = []
        seen = set()
        for message in reversed(getattr(conversation, "messages", []) or []):
            for ref in reversed(list(getattr(message, "content_refs", []) or [])):
                key = str(getattr(ref, "ref", "") or "")
                if key and key not in seen:
                    refs.append(ref)
                    seen.add(key)
        return refs

    @staticmethod
    def _file_changes(conversation: Conversation) -> list[dict]:
        latest: dict[str, dict] = {}
        labels = {
            "file__write": "已写入",
            "file__edit": "已编辑",
            "file__patch": "已应用补丁",
            "file__delete": "已删除",
        }
        action_labels = {
            "write": "已写入",
            "edit": "已编辑",
            "patch": "已应用补丁",
            "delete": "已删除",
        }
        for message in reversed(getattr(conversation, "messages", []) or []):
            for tool_call in reversed(list(getattr(message, "tool_calls", None) or [])):
                function = tool_call.get("function") if isinstance(tool_call, dict) else None
                function = function if isinstance(function, dict) else {}
                name = str(function.get("name") or "")
                if name not in labels:
                    continue
                result = tool_call.get("result") if isinstance(tool_call, dict) else None
                if not isinstance(result, dict):
                    continue
                result_metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
                if bool(result.get("is_error")) or bool(result_metadata.get("is_error")):
                    continue
                structured_change = result_metadata.get("file_change")
                change: FileChange | None = None
                if isinstance(structured_change, dict):
                    try:
                        candidate = FileChange.from_dict(structured_change)
                        if candidate.is_successful and candidate.path:
                            change = candidate
                    except (TypeError, ValueError):
                        change = None
                if change is not None:
                    path = change.path
                    deleted = change.action == "delete"
                    target = Path(path).expanduser() if path else None
                    if target is not None and not target.is_absolute():
                        target = Path(str(getattr(conversation, "work_dir", "") or ".")) / target
                    if (
                        path
                        and path not in latest
                        and (deleted or (target is not None and target.is_file()))
                    ):
                        latest[path] = {
                            "path": path,
                            "label": action_labels.get(change.action, "已变更"),
                            "deleted": deleted,
                            "action": change.action,
                            "status": change.status,
                            "summary": change.summary,
                            "change_id": change.change_id,
                            "before_digest": change.before_digest,
                            "after_digest": change.after_digest,
                        }
                    continue
                raw_arguments = function.get("arguments")
                if isinstance(raw_arguments, dict):
                    arguments = raw_arguments
                else:
                    try:
                        arguments = json.loads(str(raw_arguments or "{}"))
                    except Exception:
                        arguments = {}
                path = str(arguments.get("path") or "").strip()
                deleted = name == "file__delete"
                target = Path(path).expanduser() if path else None
                if target is not None and not target.is_absolute():
                    target = Path(str(getattr(conversation, "work_dir", "") or ".")) / target
                if (
                    path
                    and path not in latest
                    and (deleted or (target is not None and target.is_file()))
                ):
                    latest[path] = {
                        "path": path,
                        "label": labels[name],
                        "deleted": deleted,
                        "action": {
                            "file__write": "write",
                            "file__edit": "edit",
                            "file__patch": "patch",
                            "file__delete": "delete",
                        }.get(name, "edit"),
                    }
        return list(latest.values())

    def _create_input_row(self, ref) -> WorkflowCapsuleRow:
        path = None
        if self._content_service is not None and self._conversation is not None:
            try:
                if self._content_resolver is not None:
                    path = self._content_resolver.resolve(self._conversation, ref)
            except Exception:
                path = None
        row = WorkflowCapsuleRow(
            kind="input",
            status="completed" if path is not None else "failed",
            payload=ref,
            file_path=str(path or ""),
        )
        row.set_content(
            icon=Icons.get_muted(Icons.FILE_LINES),
            title=str(getattr(ref, "name", "") or "附件"),
            meta="输入",
        )
        row.setToolTip(
            f"{getattr(ref, 'name', '附件')}\n{getattr(ref, 'ref', '')}\n"
            f"{getattr(ref, 'mime', '')} · {getattr(ref, 'size', 0)} bytes"
        )
        row.set_interactive(path is not None)
        row.clicked.connect(lambda _payload, capsule=row: capsule.open_file())
        return row

    def _create_delivery_row(self, ref) -> WorkflowCapsuleRow:
        path = None
        if self._content_resolver is not None and self._conversation is not None:
            try:
                path = self._content_resolver.resolve(self._conversation, ref)
            except Exception:
                path = None
        row = WorkflowCapsuleRow(
            kind="output",
            status="completed" if path is not None else "failed",
            payload=ref,
            file_path=str(path or ""),
            work_dir=str(getattr(self._conversation, "work_dir", "") or ""),
        )
        row.set_content(
            icon=Icons.get_muted(Icons.FILE_LINES),
            title=str(getattr(ref, "name", "") or "文件"),
            meta="交付",
        )
        row.setToolTip(
            f"{getattr(ref, 'name', '文件')}\n{getattr(ref, 'ref', '')}\n"
            f"{getattr(ref, 'mime', '')} · {getattr(ref, 'size', 0)} bytes"
        )
        row.set_interactive(path is not None)
        row.clicked.connect(lambda _payload, capsule=row: capsule.open_file())
        return row

    def _create_file_change_row(self, change: dict) -> WorkflowCapsuleRow:
        path = str(change.get("path") or "")
        deleted = bool(change.get("deleted"))
        row = WorkflowCapsuleRow(
            kind="file",
            status="failed" if deleted else "completed",
            payload=path,
            file_path=path,
            work_dir=str(getattr(self._conversation, "work_dir", "") or ""),
        )
        row.set_content(
            icon="x" if deleted else "±",
            title=os.path.basename(path.replace("\\", "/")) or path,
            meta=str(change.get("label") or "变更"),
        )
        tooltip = [path]
        summary = str(change.get("summary") or "").strip()
        if summary:
            tooltip.append(summary)
        before_digest = str(change.get("before_digest") or "").strip()
        after_digest = str(change.get("after_digest") or "").strip()
        if before_digest or after_digest:
            tooltip.append(f"前: {before_digest or '-'}\n后: {after_digest or '-'}")
        row.setToolTip("\n".join(tooltip))
        row.set_interactive(not deleted)
        row.clicked.connect(lambda _payload, capsule=row: capsule.open_file())
        return row

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
        self._process_snapshots = list(snapshots or [])
        count = len(self._process_snapshots)
        self.processes_section.set_title("Shell 进程" if count == 0 else f"Shell 进程 ({count})")
        self.processes_section.set_summary("无后台进程" if count == 0 else f"{count} 个进行中")
        self.processes_section.set_collapsed(count == 0)
        self.processes_count_label.setText(f"{count} 个进行中")
        self.stop_all_processes_btn.setVisible(count > 0)

        self._clear_layout(self.processes_layout)
        for snapshot in self._process_snapshots:
            self.processes_layout.addWidget(self._create_process_row(snapshot))

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
        meta_label.setProperty("muted", True)
        meta_label.setMinimumWidth(0)
        meta_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        meta_label.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        text_column.addWidget(meta_label)
        row_layout.addLayout(text_column, 1)

        stop_btn = QToolButton()
        stop_btn.setObjectName("process_stop_btn")
        stop_btn.setIcon(Icons.get(Icons.STOP, color=Icons.COLOR_ERROR, scale_factor=0.85))
        stop_btn.setAutoRaise(True)
        stop_btn.setFixedSize(20, 20)
        stop_btn.setToolTip("停止该进程")
        process_id = str(getattr(snapshot, "process_id", "") or "")
        stop_btn.clicked.connect(lambda _checked=False, pid=process_id: self.process_stop_requested.emit(pid))
        row_layout.addWidget(stop_btn, 0, Qt.AlignmentFlag.AlignTop)
        return row

    def update_stats(self, conversation: Optional[Conversation]):
        previous_key = self._conversation_workspace_key(self._conversation)
        self._conversation = conversation
        if self._conversation_workspace_key(conversation) != previous_key:
            self._invalidate_workspace_memory()
        self._set_task_controls_enabled(bool(conversation))
        self._render_tasks(conversation)
        self._render_memory(conversation)
        self._render_artifacts(conversation)
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
        if fields & {"memory", "memory_candidates"}:
            self._invalidate_workspace_memory()
            self._render_memory(conversation)
        if fields & {"artifacts", "messages"}:
            self._render_artifacts(conversation)
        if "messages" in fields:
            self._render_channels(conversation)

    def update_app_state(self, app_state) -> None:
        previous_sources = tuple(
            getattr(self._app_state, "selected_memory_sources", ("session", "workspace")) or ()
        )
        next_sources = tuple(
            getattr(app_state, "selected_memory_sources", ("session", "workspace")) or ()
        )
        self._app_state = app_state
        if previous_sources != next_sources:
            self._render_memory(self._conversation)

    @staticmethod
    def _conversation_workspace_key(conversation: Optional[Conversation]) -> str:
        if conversation is None:
            return ""
        return str(getattr(conversation, "work_dir", "") or "").strip()

    def _invalidate_workspace_memory(self) -> None:
        self._workspace_memory_key = ""
        self._workspace_memory_count = None

    def _get_workspace_memory_count(self, conversation: Conversation) -> int:
        key = self._conversation_workspace_key(conversation)
        if self._workspace_memory_count is None or key != self._workspace_memory_key:
            self._workspace_memory_key = key
            self._workspace_memory_count = len(MemoryStats.workspace_items(key))
        return int(self._workspace_memory_count or 0)

    @staticmethod
    def _memory_sources_text(sources: tuple[str, ...]) -> str:
        labels = {
            "session": "会话",
            "workspace": "工作区",
        }
        if not sources:
            return "已禁用"
        return " / ".join(labels.get(source, source) for source in sources)

    def _add_channel_row(self, title_text: str, detail_text: str) -> None:
        row = QFrame()
        row.setObjectName("channel_origin_row")
        row.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        row_layout = QVBoxLayout(row)
        row_layout.setContentsMargins(0, 2, 0, 2)
        row_layout.setSpacing(1)

        title = QLabel(self._channel_row_title(str(title_text or "通道")))
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

    def _create_artifact_row(self, name: str, doc) -> WorkflowCapsuleRow:
        path = str(getattr(doc, "content_path", "") or "").strip()
        work_dir = str(getattr(self._conversation, "work_dir", "") or "").strip() or "."
        row = create_artifact_capsule(name, doc, work_dir=work_dir)
        row.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        row.clicked.connect(lambda target: self.artifact_open_requested.emit(target))
        return row

    def _open_artifact_path(self, path: str) -> None:
        clean = str(path or "").strip()
        if not clean:
            return
        work_dir = "."
        if self._conversation is not None:
            work_dir = str(getattr(self._conversation, "work_dir", "") or "").strip() or "."
        resolved = ArtifactService.resolve_content_path(clean, work_dir=work_dir)
        if resolved.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(resolved.resolve())))


class MemoryStats:
    @staticmethod
    def workspace_items(work_dir: str) -> list[object]:
        try:
            from core.memory.service import MemoryService

            return MemoryService.load_workspace_memory(work_dir, limit=20)
        except Exception:
            return []
