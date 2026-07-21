"""Right inspector panel for conversation state and runtime signals."""

from __future__ import annotations

from datetime import datetime
import os
from typing import Optional

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
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
from models.provider import Provider
from models.contracts.session_state import TodoStatus
from core.llm.token_budget import build_token_usage_snapshot, format_token_count
from core.channel import channel_origin_from_message
from core.state.artifact import ArtifactService
from gui.widgets.collapsible_section import CollapsibleSection
from gui.widgets.common import MetricCard
from gui.widgets.themed_line_edit import ThemedLineEdit
from gui.widgets.workflow_capsule import (
    WorkflowCapsuleRow,
    create_artifact_capsule,
)
from gui.utils.icon_manager import Icons


class InspectorMetricCard(MetricCard):
    """Inspector-specific metric card."""

    pass


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


class InspectorPanel(QWidget):
    """Panel displaying conversation state, workspace artifacts, and runtime metrics."""

    artifact_open_requested = pyqtSignal(str)
    task_create_requested = pyqtSignal(str)
    task_complete_requested = pyqtSignal(str)
    task_delete_requested = pyqtSignal(str)
    memory_candidate_promote_requested = pyqtSignal(str)
    memory_candidate_reject_requested = pyqtSignal(str)
    debug_trace_requested = pyqtSignal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("inspector_panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(260)
        self._conversation: Optional[Conversation] = None
        self._app_state = None
        self._providers: list[Provider] = []
        self._setup_ui()

    def set_providers(self, providers: list[Provider] | None) -> None:
        self._providers = list(providers or [])
        if self._conversation:
            self.update_stats(self._conversation)
    
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

        self.memory_section = CollapsibleSection("记忆", summary="会话 / 工作区", collapsed=True)
        layout.addWidget(self.memory_section)

        self.memory_container = QFrame()
        self.memory_container.setObjectName("memory_container")
        self.memory_layout = QVBoxLayout(self.memory_container)
        self.memory_layout.setContentsMargins(0, 0, 0, 0)
        self.memory_layout.setSpacing(4)
        self.memory_section.body_layout.addWidget(self.memory_container)

        self.documents_section = CollapsibleSection("产物", summary="暂无会话产物", collapsed=True)
        layout.addWidget(self.documents_section)

        self.documents_container = QFrame()
        self.documents_container.setObjectName("documents_container")
        self.documents_layout = QVBoxLayout(self.documents_container)
        self.documents_layout.setContentsMargins(0, 0, 0, 0)
        self.documents_layout.setSpacing(4)
        self.documents_section.body_layout.addWidget(self.documents_container)

        self.channels_section = CollapsibleSection("通道", summary="外部来源", collapsed=True)
        layout.addWidget(self.channels_section)

        self.channels_container = QFrame()
        self.channels_container.setObjectName("channels_container")
        self.channels_layout = QVBoxLayout(self.channels_container)
        self.channels_layout.setContentsMargins(0, 0, 0, 0)
        self.channels_layout.setSpacing(4)
        self.channels_section.body_layout.addWidget(self.channels_container)

        self.overview_section = CollapsibleSection("会话概览", summary="模式 / 核心指标", collapsed=True)
        layout.addWidget(self.overview_section)

        self.mode_card = InspectorMetricCard("模式")
        self.overview_section.body_layout.addWidget(self.mode_card)

        self.capabilities_card = InspectorMetricCard("能力")
        self.overview_section.body_layout.addWidget(self.capabilities_card)
        
        self.total_messages = InspectorMetricCard("消息数量")
        self.overview_section.body_layout.addWidget(self.total_messages)
        
        self.context_summary = InspectorMetricCard("上下文")
        self.overview_section.body_layout.addWidget(self.context_summary)
        
        self.performance_summary = InspectorMetricCard("性能")
        self.overview_section.body_layout.addWidget(self.performance_summary)

        self.timeline_section = CollapsibleSection("调试时间线", summary="空闲", collapsed=True)
        layout.addWidget(self.timeline_section)

        timeline_action_row = QHBoxLayout()
        timeline_action_row.setContentsMargins(0, 0, 0, 0)
        timeline_action_row.addStretch(1)
        self.open_trace_btn = QToolButton()
        self.open_trace_btn.setObjectName("open_trace_btn")
        self.open_trace_btn.setIcon(Icons.get_muted(Icons.NETWORK, scale_factor=0.85))
        self.open_trace_btn.setAutoRaise(True)
        self.open_trace_btn.setFixedSize(22, 22)
        self.open_trace_btn.setToolTip("查看完整调用链路")
        self.open_trace_btn.clicked.connect(self.debug_trace_requested.emit)
        timeline_action_row.addWidget(self.open_trace_btn)
        self.timeline_section.body_layout.addLayout(timeline_action_row)

        self.timeline_container = QFrame()
        self.timeline_container.setObjectName("timeline_container")
        self.timeline_layout = QVBoxLayout(self.timeline_container)
        self.timeline_layout.setContentsMargins(0, 0, 0, 0)
        self.timeline_layout.setSpacing(4)
        self.timeline_section.body_layout.addWidget(self.timeline_container)
        
        layout.addStretch(1)

        self._render_tasks(None)
        self._render_memory(None)
        self._render_artifacts(None)
        self._render_channels(None)
        self.update_runtime_state(None)
        self._set_task_controls_enabled(False)
        self.artifact_open_requested.connect(self._open_artifact_path)

    def _set_task_controls_enabled(self, enabled: bool) -> None:
        self.task_input_edit.setEnabled(bool(enabled))
        self.add_task_btn.setEnabled(bool(enabled))

    def _emit_create_task(self) -> None:
        if not self._conversation:
            return
        text = (self.task_input_edit.text() or "").strip()
        if not text:
            return
        self.task_input_edit.setText("")
        self.task_create_requested.emit(text)

    def _render_tasks(self, conversation: Optional[Conversation]) -> None:
        # clear
        while self.tasks_layout.count():
            item = self.tasks_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        if not conversation:
            self.tasks_section.set_title("任务")
            self.tasks_section.set_summary("当前任务与待办")
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

        tasks = active_tasks + list(reversed(recent_tasks[-4:]))
        active_count = len(active_tasks)
        self.tasks_section.set_title(f"任务 ({active_count})")
        self.tasks_section.set_summary("当前会话待办" if active_count else ("最近已完成" if tasks else "暂无任务"))
        if not tasks:
            empty = QLabel("暂无任务")
            empty.setProperty("muted", True)
            self.tasks_layout.addWidget(empty)
            return

        # show top N for compactness
        max_show = 8
        shown = tasks[:max_show]
        rest = len(tasks) - len(shown)

        for t in shown:
            row = QFrame()
            row.setObjectName("task_card")
            row.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(5, 2, 4, 2)
            row_layout.setSpacing(5)

            status = self._todo_status(getattr(t, "status", TodoStatus.PENDING))
            status_label = self._todo_status_label(status)
            title = str(getattr(t, "title", "") or "").strip() or "未命名任务"

            icon = QLabel()
            icon.setObjectName("task_status_icon")
            icon.setProperty("status", status.value)
            icon.setFixedSize(16, 16)
            icon.setToolTip(status_label)
            icon.setPixmap(self._todo_status_icon(status).pixmap(16, 16))
            row_layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignVCenter)

            lbl = QLabel(title)
            lbl.setObjectName("task_text")
            lbl.setWordWrap(False)
            lbl.setMinimumWidth(0)
            lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            tooltip_parts = [f"状态：{status_label}", title]
            description = str(getattr(t, "description", "") or "").strip()
            blocked_reason = str(getattr(t, "blocked_reason", "") or "").strip()
            if description:
                tooltip_parts.append(description)
            if blocked_reason:
                tooltip_parts.append(f"阻塞：{blocked_reason}")
            lbl.setToolTip("\n".join(tooltip_parts))
            lbl.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
            row_layout.addWidget(lbl, 1)

            task_id = str(getattr(t, "id", "") or "").strip()
            if task_id and status not in {TodoStatus.COMPLETED, TodoStatus.CANCELLED}:
                done_btn = QToolButton()
                done_btn.setObjectName("task_done_btn")
                done_btn.setIcon(Icons.get_success(Icons.CHECK))
                done_btn.setAutoRaise(True)
                done_btn.setFixedSize(18, 18)
                done_btn.setToolTip("标记完成")
                done_btn.clicked.connect(lambda _=False, task_id=task_id: self.task_complete_requested.emit(task_id))
                row_layout.addWidget(done_btn)

            if task_id:
                del_btn = QToolButton()
                del_btn.setObjectName("task_delete_btn")
                del_btn.setIcon(Icons.get_error(Icons.XMARK))
                del_btn.setAutoRaise(True)
                del_btn.setFixedSize(18, 18)
                del_btn.setToolTip("删除")
                del_btn.clicked.connect(lambda _=False, task_id=task_id: self.task_delete_requested.emit(task_id))
                row_layout.addWidget(del_btn)

            self.tasks_layout.addWidget(row)

        if rest > 0:
            more = QLabel(f"+{rest} 个任务未显示")
            more.setProperty("muted", True)
            self.tasks_layout.addWidget(more)

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

        source_label = QLabel(f"来源：{self._memory_sources_text(selected_sources)}")
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
            workspace_count = len(MemoryStats.workspace_items(getattr(conversation, "work_dir", "") or ""))
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
                promote.setIcon(Icons.get_success(Icons.CHECK))
                promote.setToolTip("提升为正式记忆")
                promote.setFixedSize(20, 20)
                promote.clicked.connect(
                    lambda _checked=False, candidate_id=candidate_id: self.memory_candidate_promote_requested.emit(candidate_id)
                )
                title_row.addWidget(promote)
                reject = QToolButton()
                reject.setIcon(Icons.get_error(Icons.XMARK))
                reject.setToolTip("拒绝候选")
                reject.setFixedSize(20, 20)
                reject.clicked.connect(
                    lambda _checked=False, candidate_id=candidate_id: self.memory_candidate_reject_requested.emit(candidate_id)
                )
                title_row.addWidget(reject)
                card_layout.addLayout(title_row)

                content_label = QLabel(content or "-")
                content_label.setWordWrap(True)
                content_label.setMinimumWidth(0)
                content_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                content_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                tooltip = "\n".join(part for part in (reason, *refs) if part)
                if tooltip:
                    content_label.setToolTip(tooltip)
                card_layout.addWidget(content_label)
                if refs:
                    refs_label = QLabel("引用：" + "，".join(_soft_wrap_reference(ref) for ref in refs[:3]))
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

            workspace_detail = QLabel(f"已发现 {visible_workspace_count} 条 `.pycat/memory` 记忆条目")
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
            value_label = QLabel(preview or "-")
            value_label.setWordWrap(True)
            value_label.setProperty("muted", True)
            value_label.setToolTip(str(value or "") or preview or "-")
            value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            card_layout.addWidget(value_label)

            self.memory_layout.addWidget(card)

    def _render_artifacts(self, conversation: Optional[Conversation]) -> None:
        self._clear_layout(self.documents_layout)

        if not conversation:
            self.documents_section.set_title("产物")
            self.documents_section.set_summary("暂无会话产物")
            empty = QLabel("-")
            empty.setProperty("muted", True)
            self.documents_layout.addWidget(empty)
            return

        try:
            artifacts = dict((conversation.get_state().artifacts or {}))
        except Exception:
            artifacts = {}

        self.documents_section.set_title(f"产物 ({len(artifacts)})")
        self.documents_section.set_summary("文件与草稿" if artifacts else "暂无会话产物")
        self.documents_section.set_collapsed(not bool(artifacts))
        if not artifacts:
            empty = QLabel("暂无会话产物")
            empty.setProperty("muted", True)
            self.documents_layout.addWidget(empty)
            return

        for name, doc in list(artifacts.items())[:6]:
            self.documents_layout.addWidget(self._create_artifact_row(str(name), doc))

    def _render_channels(self, conversation: Optional[Conversation]) -> None:
        self._clear_layout(self.channels_layout)
        configured_sources = tuple(getattr(self._app_state, "enabled_channel_sources", ()) or ())

        if not conversation:
            if configured_sources:
                self.channels_section.set_title(f"通道 ({len(configured_sources)})")
                self.channels_section.set_summary("已配置外部来源")
                for source in configured_sources:
                    self._add_channel_card(source, "已配置")
            else:
                self.channels_section.set_title("通道")
                self.channels_section.set_summary("暂无外部通道")
                empty = QLabel("-")
                empty.setProperty("muted", True)
                self.channels_layout.addWidget(empty)
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
        if configured_sources:
            count = len(origins) if origins else len(configured_sources)
            self.channels_section.set_title(f"通道 ({count})")
            self.channels_section.set_summary("外部来源")
        else:
            self.channels_section.set_title(f"通道 ({len(origins)})")
            self.channels_section.set_summary("外部来源")
        if not origins:
            if configured_sources:
                for source in configured_sources:
                    self._add_channel_card(source, "已配置")
            else:
                empty = QLabel("暂无外部通道")
                empty.setProperty("muted", True)
                self.channels_layout.addWidget(empty)
            return

        for origin in origins:
            details = [value for value in (origin.thread_id, origin.message_id) if value]
            self._add_channel_card(
                origin.display_name,
                "\n".join(details) if details else origin.source,
            )

        inactive_sources = [
            source for source in configured_sources
            if source not in {origin.source for origin in origins}
        ]
        for source in inactive_sources[:4]:
            self._add_channel_card(source, "已配置")
    
    def update_stats(self, conversation: Optional[Conversation]):
        self._conversation = conversation
        self._set_task_controls_enabled(bool(conversation))
        self._render_tasks(conversation)
        self._render_memory(conversation)
        self._render_artifacts(conversation)
        self._render_channels(conversation)
        if not conversation:
            self._clear_stats()
            return

        self.mode_card.set_value(str(getattr(conversation, "mode", "") or "chat"))
        settings = getattr(conversation, "settings", {}) or {}
        flags = []
        if settings.get("show_thinking", True):
            flags.append("思考")
        self.capabilities_card.set_value(" / ".join(flags) if flags else "对话")
        
        msg_count = len(conversation.messages)
        self.total_messages.set_value(str(msg_count))
        snapshot = build_token_usage_snapshot(conversation, providers=self._providers)
        if snapshot is not None:
            used = format_token_count(snapshot.context_tokens)
            window = format_token_count(snapshot.context_window)
            self.context_summary.set_value(
                f"{used}/{window} · {snapshot.usage_ratio * 100:.1f}%"
            )
        else:
            self.context_summary.set_value("-")

        tpm = conversation.get_tokens_per_minute()
        performance_parts = []
        if tpm > 0:
            performance_parts.append(f"{tpm:.1f} token/min")
        
        last_assistant = None
        for msg in reversed(conversation.messages):
            if msg.role == 'assistant' and msg.response_time_ms:
                last_assistant = msg
                break
        
        if last_assistant and last_assistant.response_time_ms:
            time_sec = last_assistant.response_time_ms / 1000
            performance_parts.append(f"{time_sec:.2f}s")
        self.performance_summary.set_value(" · ".join(performance_parts) if performance_parts else "-")
        
        self.overview_section.set_summary(str(getattr(conversation, "mode", "chat") or "chat"))
    
    def update_streaming_stats(self, tokens: int, elapsed_ms: int):
        self.context_summary.set_value(format_token_count(tokens))
        if elapsed_ms > 0:
            tpm = (tokens / elapsed_ms) * 60000
            time_sec = elapsed_ms / 1000
            self.performance_summary.set_value(f"{tpm:.1f} token/min · {time_sec:.2f}s")

    def update_app_state(self, app_state) -> None:
        self._app_state = app_state
        self._render_memory(self._conversation)
        self._render_channels(self._conversation)

    def update_runtime_state(self, stream_state) -> None:
        if stream_state is None:
            self.timeline_section.set_summary("空闲")
            self._render_runtime_events(None)
            return

        active_tool = str(getattr(stream_state, "active_tool", "") or "").strip()
        last_kind = str(getattr(stream_state, "last_event_kind", "") or "").strip()
        last_detail = str(getattr(stream_state, "last_event_detail", "") or "").strip()
        if active_tool:
            self.timeline_section.set_summary(f"工具中 · {active_tool}")
        elif last_kind:
            self.timeline_section.set_summary(self._runtime_event_label(last_kind))
        else:
            model = str(getattr(stream_state, "model", "") or "").strip()
            self.timeline_section.set_summary(model or "生成中")

        self._render_runtime_events(stream_state)

    @staticmethod
    def _runtime_event_label(kind: str) -> str:
        labels = {
            "turn_start": "新一轮开始",
            "tool_start": "工具开始",
            "tool_end": "工具完成",
            "retry": "重试中",
            "complete": "已完成",
            "error": "出错",
            "step": "写入步骤",
        }
        return labels.get(str(kind or ""), str(kind or "运行中"))

    def _render_runtime_events(self, stream_state) -> None:
        self._clear_layout(self.timeline_layout)

        if stream_state is None:
            self.timeline_section.set_title("调试时间线")
            empty = QLabel("暂无运行事件")
            empty.setProperty("muted", True)
            self.timeline_layout.addWidget(empty)
            return

        events = list(getattr(stream_state, "recent_events", []) or [])
        self.timeline_section.set_title(f"调试时间线 ({len(events)})")
        if not events:
            empty = QLabel("等待本轮事件写入")
            empty.setProperty("muted", True)
            self.timeline_layout.addWidget(empty)
            return

        for event in reversed(events[-8:]):
            card = QFrame()
            card.setObjectName("task_card")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(10, 8, 10, 8)
            card_layout.setSpacing(4)

            title = QLabel(self._runtime_event_title(event))
            title.setObjectName("task_text")
            title.setWordWrap(True)
            card_layout.addWidget(title)

            meta_text = self._runtime_event_meta(event)
            if meta_text:
                meta = QLabel(meta_text)
                meta.setProperty("muted", True)
                meta.setWordWrap(True)
                card_layout.addWidget(meta)

            detail_text = str(event.get("detail") or event.get("summary") or "-")
            detail = QLabel(detail_text)
            detail.setProperty("muted", True)
            detail.setWordWrap(True)
            card_layout.addWidget(detail)

            self.timeline_layout.addWidget(card)

    def _runtime_event_title(self, event: dict) -> str:
        kind = str(event.get("kind") or "").strip()
        label = self._runtime_event_label(kind)
        tool_name = str(event.get("tool_name") or event.get("name") or "").strip()
        role = str(event.get("role") or "").strip()

        if kind == "step" and role == "tool_result":
            return f"工具回写 · {tool_name or 'tool'}"
        if kind == "step" and role == "assistant":
            return "助手消息"
        if tool_name and kind in {"tool_start", "tool_end"}:
            return f"{label} · {tool_name}"
        return label

    def _runtime_event_meta(self, event: dict) -> str:
        parts: list[str] = []
        try:
            turn = int(event.get("turn") or 0)
        except Exception:
            turn = 0
        if turn > 0:
            parts.append(f"T{turn}")

        timestamp = event.get("recorded_at")
        try:
            if timestamp:
                parts.append(datetime.fromtimestamp(float(timestamp)).strftime("%H:%M:%S"))
        except Exception:
            pass

        phase = str(event.get("phase") or "").strip()
        if phase and phase not in {"start", "end"}:
            parts.append(phase)

        return " · ".join(parts)
    
    def _clear_stats(self):
        self.overview_section.set_summary("模式 / 核心指标")
        self.mode_card.set_value("-")
        self.capabilities_card.set_value("-")
        self.total_messages.set_value("-")
        self.context_summary.set_value("-")
        self.performance_summary.set_value("-")
        self.update_runtime_state(None)

    @staticmethod
    def _memory_sources_text(sources: tuple[str, ...]) -> str:
        labels = {
            "session": "会话",
            "workspace": "工作区",
        }
        if not sources:
            return "已禁用"
        return " / ".join(labels.get(source, source) for source in sources)

    def _add_channel_card(self, title_text: str, detail_text: str) -> None:
        card = QFrame()
        card.setObjectName("task_card")
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(10, 8, 10, 8)
        card_layout.setSpacing(4)

        title = QLabel(self._channel_card_title(str(title_text or "通道")))
        title.setObjectName("task_text")
        title.setWordWrap(True)
        title.setMinimumWidth(0)
        title.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        title.setToolTip(str(title_text or "通道"))
        card_layout.addWidget(title)

        detail_label = _TwoLineElideLabel(str(detail_text or "-"))
        detail_label.setProperty("muted", True)
        detail_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        detail_label.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        card_layout.addWidget(detail_label)

        self.channels_layout.addWidget(card)

    @staticmethod
    def _channel_card_title(text: str) -> str:
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
