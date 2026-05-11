"""Right sidebar panel.

Shows the current conversation's active tasks (SessionState) and metrics.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QFrame,
    QToolButton,
    QLineEdit,
    QScrollArea,
)

from models.conversation import Conversation
from models.state import TaskStatus
from core.channel import channel_origin_from_message
from ui.widgets.collapsible_section import CollapsibleSection
from ui.utils.icon_manager import Icons


class StatCard(QFrame):
    """A card displaying a single statistic"""
    
    def __init__(self, label: str, value: str = "-", parent=None):
        super().__init__(parent)
        self.setObjectName("stat_card")
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)
        
        self.label = QLabel(label)
        self.label.setObjectName("stat_label")
        layout.addWidget(self.label)
        
        self.value_label = QLabel(value)
        self.value_label.setObjectName("stat_value")
        layout.addWidget(self.value_label)
    
    def set_value(self, value: str):
        self.value_label.setText(value)


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


class StatsPanel(QWidget):
    """Panel displaying conversation state + statistics."""

    task_create_requested = pyqtSignal(str)
    task_complete_requested = pyqtSignal(str)
    task_delete_requested = pyqtSignal(str)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("stats_panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(260)
        self._conversation: Optional[Conversation] = None
        self._app_state = None
        self._setup_ui()
    
    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("stats_scroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        root.addWidget(self.scroll)

        content = QWidget()
        content.setObjectName("stats_scroll_content")
        content.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.scroll.setWidget(content)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.tasks_section = CollapsibleSection("任务", summary="当前任务与待办", collapsed=False)
        layout.addWidget(self.tasks_section)

        add_row = QHBoxLayout()
        add_row.setSpacing(6)

        self.task_input_edit = QLineEdit()
        self.task_input_edit.setObjectName("task_input_edit")
        self.task_input_edit.setPlaceholderText("新增任务…")
        self.task_input_edit.returnPressed.connect(self._emit_create_task)
        add_row.addWidget(self.task_input_edit, 1)

        self.add_task_btn = QToolButton()
        self.add_task_btn.setObjectName("add_task_btn")
        self.add_task_btn.setIcon(Icons.get(Icons.PLUS, scale_factor=0.8))
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
        self.tasks_layout.setSpacing(6)
        self.tasks_section.body_layout.addWidget(self.tasks_container)

        self.memory_section = CollapsibleSection("记忆", summary="会话与工作区记忆摘要", collapsed=True)
        layout.addWidget(self.memory_section)

        self.memory_container = QFrame()
        self.memory_container.setObjectName("memory_container")
        self.memory_layout = QVBoxLayout(self.memory_container)
        self.memory_layout.setContentsMargins(0, 0, 0, 0)
        self.memory_layout.setSpacing(6)
        self.memory_section.body_layout.addWidget(self.memory_container)

        self.documents_section = CollapsibleSection("产物", summary="会话产物摘要", collapsed=True)
        layout.addWidget(self.documents_section)

        self.documents_container = QFrame()
        self.documents_container.setObjectName("documents_container")
        self.documents_layout = QVBoxLayout(self.documents_container)
        self.documents_layout.setContentsMargins(0, 0, 0, 0)
        self.documents_layout.setSpacing(6)
        self.documents_section.body_layout.addWidget(self.documents_container)

        self.channels_section = CollapsibleSection("通道", summary="外部来源", collapsed=True)
        layout.addWidget(self.channels_section)

        self.channels_container = QFrame()
        self.channels_container.setObjectName("channels_container")
        self.channels_layout = QVBoxLayout(self.channels_container)
        self.channels_layout.setContentsMargins(0, 0, 0, 0)
        self.channels_layout.setSpacing(6)
        self.channels_section.body_layout.addWidget(self.channels_container)

        self.overview_section = CollapsibleSection("会话概览", summary="模式 / 核心指标", collapsed=False)
        layout.addWidget(self.overview_section)

        self.mode_card = StatCard("模式")
        self.overview_section.body_layout.addWidget(self.mode_card)

        self.capabilities_card = StatCard("能力")
        self.overview_section.body_layout.addWidget(self.capabilities_card)
        
        self.total_messages = StatCard("消息数量")
        self.overview_section.body_layout.addWidget(self.total_messages)
        
        self.total_tokens = StatCard("总 Token")
        self.overview_section.body_layout.addWidget(self.total_tokens)
        
        self.tokens_per_min = StatCard("Token/分钟")
        self.overview_section.body_layout.addWidget(self.tokens_per_min)
        
        self.last_response_time = StatCard("最近响应时间")
        self.overview_section.body_layout.addWidget(self.last_response_time)

        self.timeline_section = CollapsibleSection("调试时间线", summary="空闲", collapsed=True)
        layout.addWidget(self.timeline_section)

        self.timeline_container = QFrame()
        self.timeline_container.setObjectName("timeline_container")
        self.timeline_layout = QVBoxLayout(self.timeline_container)
        self.timeline_layout.setContentsMargins(0, 0, 0, 0)
        self.timeline_layout.setSpacing(6)
        self.timeline_section.body_layout.addWidget(self.timeline_container)
        
        layout.addStretch(1)

        self._render_tasks(None)
        self._render_memory(None)
        self._render_artifacts(None)
        self._render_channels(None)
        self.update_runtime_state(None)
        self._set_task_controls_enabled(False)

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
            active_tasks = list(state.get_active_tasks() or [])
        except Exception:
            active_tasks = []

        self.tasks_section.set_title(f"任务 ({len(active_tasks)})")
        self.tasks_section.set_summary("当前会话待办" if active_tasks else "暂无进行中的任务")
        if not active_tasks:
            empty = QLabel("暂无进行中的任务")
            empty.setProperty("muted", True)
            self.tasks_layout.addWidget(empty)
            return

        # show top N for compactness
        max_show = 6
        shown = active_tasks[:max_show]
        rest = len(active_tasks) - len(shown)

        for t in shown:
            row = QFrame()
            row.setObjectName("task_card")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(10, 8, 10, 8)
            row_layout.setSpacing(6)

            status = "进行中" if getattr(t, "status", None) == TaskStatus.IN_PROGRESS else "待办"
            text = f"{status} · {getattr(t, 'content', '')}".strip()
            lbl = QLabel(text)
            lbl.setObjectName("task_text")
            lbl.setWordWrap(True)
            row_layout.addWidget(lbl, 1)

            done_btn = QToolButton()
            done_btn.setObjectName("task_done_btn")
            done_btn.setIcon(Icons.get_success(Icons.CHECK, scale_factor=0.75))
            done_btn.setAutoRaise(True)
            done_btn.setFixedSize(22, 22)
            done_btn.setToolTip("标记完成")
            done_btn.clicked.connect(lambda _=False, task_id=getattr(t, 'id', ''): self.task_complete_requested.emit(task_id))
            row_layout.addWidget(done_btn)

            del_btn = QToolButton()
            del_btn.setObjectName("task_delete_btn")
            del_btn.setIcon(Icons.get_error(Icons.XMARK, scale_factor=0.75))
            del_btn.setAutoRaise(True)
            del_btn.setFixedSize(22, 22)
            del_btn.setToolTip("删除")
            del_btn.clicked.connect(lambda _=False, task_id=getattr(t, 'id', ''): self.task_delete_requested.emit(task_id))
            row_layout.addWidget(del_btn)

            self.tasks_layout.addWidget(row)

        if rest > 0:
            more = QLabel(f"+{rest} 个任务未显示")
            more.setProperty("muted", True)
            self.tasks_layout.addWidget(more)

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

        if not selected_sources:
            self.memory_section.set_title("记忆 (已禁用)")
            self.memory_section.set_summary("当前会话不注入记忆")
            empty = QLabel("当前会话已禁用记忆注入。")
            empty.setProperty("muted", True)
            self.memory_layout.addWidget(empty)
            return

        if not conversation:
            empty = QLabel("-")
            empty.setProperty("muted", True)
            self.memory_layout.addWidget(empty)
            return

        try:
            memory = dict((conversation.get_state().memory or {}))
            workspace_count = len(MemoryStats.workspace_items(getattr(conversation, "work_dir", "") or ""))
        except Exception:
            memory = {}
            workspace_count = 0

        visible_memory = dict(memory) if session_enabled else {}
        visible_workspace_count = workspace_count if workspace_enabled else 0

        total_count = len(visible_memory) + visible_workspace_count
        self.memory_section.set_title(f"记忆 ({total_count})")
        self.memory_section.set_summary(self._memory_sources_text(selected_sources))
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

            preview = str(value or "")
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
            self.documents_section.set_summary("会话产物摘要")
            empty = QLabel("-")
            empty.setProperty("muted", True)
            self.documents_layout.addWidget(empty)
            return

        try:
            artifacts = dict((conversation.get_state().artifacts or {}))
        except Exception:
            artifacts = {}

        self.documents_section.set_title(f"产物 ({len(artifacts)})")
        self.documents_section.set_summary("会话产物摘要" if artifacts else "暂无会话产物")
        if not artifacts:
            empty = QLabel("暂无会话产物")
            empty.setProperty("muted", True)
            self.documents_layout.addWidget(empty)
            return

        for name, doc in list(artifacts.items())[:4]:
            card = QFrame()
            card.setObjectName("task_card")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(10, 8, 10, 8)
            card_layout.setSpacing(4)

            name_label = QLabel(str(name))
            name_label.setObjectName("task_text")
            card_layout.addWidget(name_label)

            preview = str(getattr(doc, 'abstract', '') or '') or str(getattr(doc, 'content', '') or '')
            if len(preview) > 140:
                preview = preview[:140] + "..."
            content_label = QLabel(preview or "-")
            content_label.setWordWrap(True)
            content_label.setProperty("muted", True)
            full_text = str(getattr(doc, 'content', '') or '') or preview or "-"
            content_label.setToolTip(full_text)
            content_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            card_layout.addWidget(content_label)

            refs = [str(item).strip() for item in (getattr(doc, 'references', []) or []) if str(item).strip()]
            if refs:
                refs_label = QLabel(" | ".join(refs[:2]))
                refs_label.setWordWrap(True)
                refs_label.setProperty("muted", True)
                refs_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                card_layout.addWidget(refs_label)

            self.documents_layout.addWidget(card)

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
            details = []
            if origin.thread_id:
                details.append(f"线程: {origin.thread_id}")
            if origin.message_id:
                details.append(f"消息: {origin.message_id}")
            self._add_channel_card(
                origin.display_name,
                " | ".join(details) if details else origin.source,
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
        
        total_tokens = sum(m.tokens or 0 for m in conversation.messages)
        self.total_tokens.set_value(f"{total_tokens:,}")
        
        tpm = conversation.get_tokens_per_minute()
        if tpm > 0:
            self.tokens_per_min.set_value(f"{tpm:.1f}")
        else:
            self.tokens_per_min.set_value("-")
        
        last_assistant = None
        for msg in reversed(conversation.messages):
            if msg.role == 'assistant' and msg.response_time_ms:
                last_assistant = msg
                break
        
        if last_assistant and last_assistant.response_time_ms:
            time_sec = last_assistant.response_time_ms / 1000
            self.last_response_time.set_value(f"{time_sec:.2f}s")
        else:
            self.last_response_time.set_value("-")
        
        self.overview_section.set_summary(str(getattr(conversation, "mode", "chat") or "chat"))
    
    def update_streaming_stats(self, tokens: int, elapsed_ms: int):
        self.total_tokens.set_value(f"{tokens:,}")
        if elapsed_ms > 0:
            tpm = (tokens / elapsed_ms) * 60000
            self.tokens_per_min.set_value(f"{tpm:.1f}")
            time_sec = elapsed_ms / 1000
            self.last_response_time.set_value(f"{time_sec:.2f}s")

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
        self.total_tokens.set_value("-")
        self.tokens_per_min.set_value("-")
        self.last_response_time.set_value("-")
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
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(10, 8, 10, 8)
        card_layout.setSpacing(4)

        title = QLabel(str(title_text or "通道"))
        title.setObjectName("task_text")
        card_layout.addWidget(title)

        detail_label = QLabel(str(detail_text or "-"))
        detail_label.setWordWrap(True)
        detail_label.setProperty("muted", True)
        detail_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail_label.setToolTip(str(detail_text or "-"))
        card_layout.addWidget(detail_label)

        self.channels_layout.addWidget(card)


class MemoryStats:
    @staticmethod
    def workspace_items(work_dir: str) -> list[object]:
        try:
            from core.state.services.memory_service import MemoryService

            return MemoryService.load_workspace_memory(work_dir, limit=20)
        except Exception:
            return []
