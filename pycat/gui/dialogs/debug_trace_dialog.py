from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QSize, Qt, QUrl, QThreadPool
from PyQt6.QtGui import QDesktopServices, QFontDatabase
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pycat.models.conversation import Conversation
from pycat.core.observability import resolve_debug_trace_dir
from pycat.core.observability.reader import read_trace_events, read_trace_node, MAX_EVENTS, MAX_TRACE_BYTES, MAX_PAYLOAD_BYTES
from pycat.gui.dialogs.tool_retest_dialog import ToolRetestDialog, show_sdk_dialog, tool_call_code
from pycat.gui.utils.theme import prepare_context_menu
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.display_text import single_line
from pycat.gui.widgets.capsule import SingleLineLabel
from pycat.gui.runtime.background_job import BackgroundJob
from pycat.gui.utils.window_geometry import apply_workbench_dialog_size
from pycat.gui.widgets.themed_line_edit import ThemedLineEdit, ThemedPlainTextEdit, ThemedSelectableLabel


def _short_id(value: object, *, head: int = 8, tail: int = 4) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    if len(text) <= head + tail + 1:
        return text
    return f"{text[:head]}...{text[-tail:]}"


def _pretty_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        return str(value)


class DebugTraceDialog(QDialog):
    """Read-only trace viewer backed by ``events.jsonl``."""

    FILTERS = (
        ("all", "全部"),
        ("llm", "LLM"),
        ("tool", "工具"),
        ("error", "错误"),
        ("subtask", "子任务"),
    )
    MAX_EVENTS = MAX_EVENTS
    MAX_TRACE_BYTES = MAX_TRACE_BYTES
    MAX_PAYLOAD_BYTES = MAX_PAYLOAD_BYTES

    def __init__(self, conversation: Conversation, parent=None, *, request_ids=(), state_provider=None,
                 services=None, on_tool_finished=None):
        super().__init__(parent)
        self.setWindowTitle("运行检查")
        self.setObjectName("debug_trace_dialog")
        self.setModal(False)
        apply_workbench_dialog_size(self)

        self._conversation = conversation.clone()
        self._state_provider = state_provider
        self._services = services
        self._on_tool_finished = on_tool_finished
        self._detail_job = None
        self._detail = None
        self.target = (conversation.id, tuple(request_ids))
        self._load_job = None
        self._load_note = ""
        self._search_text = {}
        self._debug_dir = self._resolve_debug_dir(conversation)
        self._events: list[dict[str, Any]] = []
        self._node_events: dict[str, list[dict[str, Any]]] = {}
        self._items: dict[str, QTreeWidgetItem] = {}
        self._active_filter = "all"

        self._build_ui()
        self.refresh()

    @staticmethod
    def _resolve_debug_dir(conversation: Conversation) -> Path:
        return resolve_debug_trace_dir(
            conversation_id=str(getattr(conversation, "id", "") or "default"),
            work_dir=str(getattr(conversation, "work_dir", "") or ""),
            data_dir=getattr(conversation, "data_dir", None),
        )

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        header = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(Icons.get(Icons.CHART_BARS).pixmap(18, 18))
        header.addWidget(icon)
        self.subtitle = SingleLineLabel(self._conversation.title)
        self.subtitle.setObjectName("trace_title")
        header.addWidget(self.subtitle, 1)

        self.refresh_btn = self._tool_button(Icons.REFRESH, "刷新")
        self.refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self.refresh_btn)

        self.copy_request_btn = self._tool_button(Icons.COPY, "复制当前节点 request_id")
        self.copy_request_btn.clicked.connect(self._copy_request_id)
        header.addWidget(self.copy_request_btn)

        self.open_dir_btn = self._tool_button(Icons.FOLDER, "打开 debug 文件夹")
        self.open_dir_btn.clicked.connect(self._open_debug_dir)
        header.addWidget(self.open_dir_btn)
        self.more_btn = self._tool_button(Icons.MORE, "更多运行操作")
        self.more_menu = prepare_context_menu(QMenu(self.more_btn), self)
        self.more_menu.addAction("SDK 用法", self._show_sdk)
        self.more_btn.setMenu(self.more_menu)
        self.more_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        header.addWidget(self.more_btn)
        root.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        self.pages = QTabWidget()
        self.pages.setObjectName("run_inspection_tabs")
        self.pages.addTab(splitter, "过程")
        state_page = QWidget()
        state_layout = QVBoxLayout(state_page)
        state_layout.setContentsMargins(0, 8, 0, 0)
        self.state_note = QLabel()
        self.state_note.setWordWrap(True)
        self.state_note.setProperty("muted", True)
        state_layout.addWidget(self.state_note)
        self.state_text = self._json_view()
        state_layout.addWidget(self.state_text, 1)
        self.pages.addTab(state_page, "当前状态")
        root.addWidget(self.pages, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)
        left_layout.setSpacing(8)

        self.search_edit = ThemedLineEdit()
        self.search_edit.setFixedHeight(32)
        self.search_edit.setAccessibleName("搜索运行节点")
        self.search_edit.setPlaceholderText("搜索节点 / 工具 / request_id")
        self.search_edit.textChanged.connect(self._apply_filter)
        left_layout.addWidget(self.search_edit)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(4)
        self.filter_buttons: dict[str, QToolButton] = {}
        for key, label in self.FILTERS:
            btn = QToolButton()
            btn.setText(label)
            btn.setCheckable(True)
            btn.setAutoRaise(True)
            btn.clicked.connect(lambda _=False, key=key: self._set_filter(key))
            self.filter_buttons[key] = btn
            filter_row.addWidget(btn)
        self.filter_buttons["all"].setChecked(True)
        filter_row.addStretch(1)
        left_layout.addLayout(filter_row)

        self.tree = QTreeWidget()
        self.tree.setObjectName("trace_tree")
        self.tree.setWordWrap(False)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.tree.setUniformRowHeights(True)
        self.tree.setColumnCount(2)
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(12)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.tree.setColumnWidth(1, 106)
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)
        left_layout.addWidget(self.tree, 1)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        right_layout.setSpacing(8)

        self.node_title = SingleLineLabel("选择一个运行节点")
        self.node_title.setObjectName("trace_node_title")
        self.node_title.setFixedHeight(28)
        node_header = QHBoxLayout()
        node_header.addWidget(self.node_title, 1)
        self.retest_btn = QPushButton("复测工具…")
        self.retest_btn.clicked.connect(self._open_retest)
        node_header.addWidget(self.retest_btn)
        self.copy_call_btn = self._tool_button(Icons.CODE, "复制调用代码")
        self.copy_call_btn.clicked.connect(self._copy_tool_call)
        node_header.addWidget(self.copy_call_btn)
        self.retest_btn.hide()
        self.copy_call_btn.hide()
        right_layout.addLayout(node_header)
        self.metrics_bar = QFrame()
        self.metrics_bar.setObjectName("trace_metrics")
        self.metrics_bar.setFixedHeight(32)
        metrics = QHBoxLayout(self.metrics_bar)
        metrics.setContentsMargins(0, 0, 0, 4)
        metrics.setSpacing(8)
        for key, caption in (("status", "状态"), ("duration", "耗时"), ("token", "Token"), ("ref", "引用")):
            label = QLabel(caption)
            label.setProperty("muted", True)
            metrics.addWidget(label)
            value = SingleLineLabel("—")
            value.setObjectName("trace_metric_value")
            metrics.addWidget(value, 1)
            setattr(self, f"{key}_value", value)
        right_layout.addWidget(self.metrics_bar)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("trace_tabs")
        self.copy_tab_btn = self._tool_button(Icons.COPY, "复制当前页")
        self.copy_tab_btn.clicked.connect(self._copy_current_tab)
        self.tabs.setCornerWidget(self.copy_tab_btn, Qt.Corner.TopRightCorner)
        self.overview_text = self._json_view()
        self.request_text = self._json_view()
        self.raw_result_text = self._json_view()
        self.model_result_text = self._json_view()
        self.event_text = self._json_view()
        self.response_text = self.model_result_text
        self.tabs.addTab(self.overview_text, "概览")
        self.tabs.addTab(self.request_text, "请求")
        self.tabs.addTab(self.raw_result_text, "原始结果")
        self.tabs.addTab(self.model_result_text, "模型视图")
        self.tabs.addTab(self.event_text, "事件")
        right_layout.addWidget(self.tabs, 1)
        splitter.addWidget(right)
        left.setMinimumWidth(260)
        right.setMinimumWidth(360)
        splitter.setHandleWidth(1)
        splitter.setSizes([300, 780])

        self.path_label = SingleLineLabel("")
        self.path_label.setProperty("muted", True)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.path_label)

    def _tool_button(self, icon_name: str, tooltip: str) -> QToolButton:
        btn = QToolButton(self)
        btn.setIcon(Icons.get_muted(icon_name, scale_factor=0.9))
        btn.setIconSize(QSize(18, 18))
        btn.setToolTip(tooltip)
        btn.setAutoRaise(True)
        btn.setFixedSize(28, 28)
        btn.setAccessibleName(tooltip)
        btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        return btn

    def _json_view(self) -> QPlainTextEdit:
        edit = ThemedPlainTextEdit()
        edit.setReadOnly(True)
        edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        edit.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        edit.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        return edit

    def refresh(self) -> None:
        current = self._state_provider() if self._state_provider else None
        live = current is not None and current.id == self._conversation.id
        snapshot = current if live else self._conversation
        self.state_note.setText("刷新时的当前会话状态，可能包含尚未保存的进度。" if live else "打开窗口时的会话快照。")
        self.state_note.setText(self.state_note.text() + " 历史请求中的状态请在过程页查看。")
        self.state_text.setPlainText(_pretty_json(snapshot.get_state().to_dict()))
        if self._load_job is not None:
            self._load_job.abandon()
        path, request_ids = self._debug_dir / "events.jsonl", self.target[1]
        job = BackgroundJob(lambda: self._read_events(path, request_ids))
        self._load_job = job
        self.subtitle.setText("正在读取运行记录…")
        self.refresh_btn.setEnabled(False)
        self.destroyed.connect(job.abandon)
        job.signals.finished.connect(lambda result, error: self._finish_load(job, result, error))
        QThreadPool.globalInstance().start(job)

    def _finish_load(self, job, result, error):
        if self._load_job is not job:
            return
        self._load_job = None
        self.refresh_btn.setEnabled(True)
        self._events, self._load_note = result if error is None else ([], f"读取失败：{error}")
        self._update_context_labels()
        current = self.tree.currentItem()
        selected_key = current.data(0, Qt.ItemDataRole.UserRole) if current else ""
        self._rebuild_tree()
        self._apply_filter()
        if selected_key in self._items and not self._items[selected_key].isHidden():
            self.tree.setCurrentItem(self._items[selected_key])
        if self.tree.topLevelItemCount() > 0 and not self.tree.selectedItems():
            first = self.tree.topLevelItem(0)
            self.tree.setCurrentItem(first)

    def closeEvent(self, event):
        if self._load_job is not None:
            self._load_job.abandon()
            self._load_job = None
        if self._detail_job is not None:
            self._detail_job.abandon()
            self._detail_job = None
        super().closeEvent(event)

    def _update_context_labels(self) -> None:
        session_id = str(getattr(self._conversation, "id", "") or "-")
        request_ids = []
        seen = set()
        for event in self._events:
            request_id = str(event.get("request_id") or "").strip()
            if request_id and request_id not in seen:
                seen.add(request_id)
                request_ids.append(request_id)
        scope = "本次运行" if self.target[1] else f"{len(request_ids)} 次运行"
        self.subtitle.setText(f"{self._conversation.title or '未命名对话'} · {scope} · {len(self._events)} 条事件")
        self.subtitle.setToolTip(
            f"{self._conversation.title}\nsession: {session_id}\nrequest: {', '.join(request_ids) or '-'}"
        )
        compact_path = self._compact_debug_path(session_id)
        self.path_label.setText(self._load_note or f"debug: {compact_path}")
        self.path_label.setToolTip(f"{self._load_note}\n{self._debug_dir}".strip())

    def _compact_debug_path(self, session_id: str) -> str:
        name = _short_id(session_id)
        return str(Path(".pycat") / "sessions" / name / "debug")

    @classmethod
    def _read_events(cls, path: Path, request_ids=()) -> tuple[list[dict[str, Any]], str]:
        page = read_trace_events(path, request_ids=request_ids, limit=cls.MAX_EVENTS, max_bytes=cls.MAX_TRACE_BYTES)
        return page["events"], page["note"]

    def _rebuild_tree(self) -> None:
        self.tree.clear()
        self._node_events = {}
        self._items = {}
        self._search_text = {}
        parent_by_node: dict[str, str] = {}
        order: list[str] = []

        for index, event in enumerate(self._events):
            raw_node_id = str(event.get("node_id") or "").strip() or f"event-{index}"
            node_key = self._node_key(event, raw_node_id)
            event["_node_key"] = node_key
            if node_key not in self._node_events:
                self._node_events[node_key] = []
                order.append(node_key)
            self._node_events[node_key].append(event)
            parent = str(event.get("parent_id") or "").strip()
            if parent and node_key not in parent_by_node:
                parent_by_node[node_key] = self._node_key(event, parent)

        if not order:
            self._show_node("")
            empty = QTreeWidgetItem(["暂无 trace 事件", ""])
            empty.setData(0, Qt.ItemDataRole.UserRole, "")
            self.tree.addTopLevelItem(empty)
            return

        for node_id in order:
            events = self._node_events.get(node_id) or []
            item = QTreeWidgetItem([single_line(self._node_title(node_id)), self._node_time_label(events)])
            item.setData(0, Qt.ItemDataRole.UserRole, node_id)
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            tooltip = self._node_time_tooltip(events)
            if tooltip:
                item.setToolTip(0, f"{self._node_title(node_id)}\n{tooltip}")
                item.setToolTip(1, tooltip)
            self._items[node_id] = item
            self._search_text[node_id] = " ".join(json.dumps(event, ensure_ascii=False).casefold() for event in events)

        for node_id in order:
            item = self._items[node_id]
            parent_id = parent_by_node.get(node_id, "")
            parent = self._items.get(parent_id)
            if parent is not None and parent is not item:
                parent.addChild(item)
            else:
                self.tree.addTopLevelItem(item)
        self.tree.expandAll()
        if self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

    @staticmethod
    def _node_key(event: dict[str, Any], node_id: str) -> str:
        request_id = str(event.get("request_id") or "").strip()
        return f"{request_id}\x1f{node_id}" if request_id else node_id

    def _node_title(self, node_id: str) -> str:
        events = self._node_events.get(node_id) or []
        first = events[0] if events else {}
        last = events[-1] if events else first
        display_node_id = node_id.rsplit("\x1f", 1)[-1]
        kind = str(first.get("kind") or last.get("kind") or "event")
        status = str(last.get("status") or "")
        name = str(first.get("name") or last.get("name") or "")
        if kind == "run":
            return f"Run {_short_id(first.get('request_id'))} · {status or 'running'}"
        if kind == "turn":
            return f"{name or display_node_id} · {status or 'running'}"
        if kind == "llm":
            return f"{display_node_id} · {name or 'main'} · {status or 'running'}"
        if kind == "tool":
            return f"{str(last.get('tool_name') or name or 'tool')} · {status or 'running'}"
        if kind == "subtask":
            return f"Subtask · {name or node_id} · {status or 'running'}"
        if kind == "condense":
            return f"Condense · {status or name or 'event'}"
        if kind == "retry":
            return f"Retry · {name or status or node_id}"
        return f"{kind} · {name or display_node_id}"

    def _node_time_label(self, events: list[dict[str, Any]]) -> str:
        if not events:
            return ""
        return self._format_event_datetime_compact(events[-1])

    def _node_time_tooltip(self, events: list[dict[str, Any]]) -> str:
        if not events:
            return ""
        start = self._format_event_timestamp(events[0])
        end = self._format_event_timestamp(events[-1])
        duration = self._format_duration(events[-1].get("duration_ms"))
        return f"开始: {start or '-'}\n结束: {end or '-'}\n耗时: {duration}"

    @classmethod
    def _format_event_datetime_compact(cls, event: dict[str, Any]) -> str:
        dt = cls._event_datetime(event)
        if dt is not None:
            return dt.strftime("%m-%d %H:%M:%S")
        ts = str(event.get("ts") or "").strip()
        if len(ts) >= 19 and "T" in ts:
            return f"{ts[5:10]} {ts[11:19]}"
        return ""

    @classmethod
    def _format_event_timestamp(cls, event: dict[str, Any]) -> str:
        dt = cls._event_datetime(event)
        if dt is not None:
            return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        return str(event.get("ts") or event.get("time") or "").strip()

    @staticmethod
    def _event_datetime(event: dict[str, Any]) -> datetime | None:
        ts = str(event.get("ts") or "").strip()
        if ts:
            try:
                return datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                pass
        try:
            value = float(event.get("time") or 0)
        except Exception:
            value = 0
        if value <= 0:
            return None
        try:
            return datetime.fromtimestamp(value)
        except Exception:
            return None

    def _set_filter(self, key: str) -> None:
        self._active_filter = key
        for name, btn in self.filter_buttons.items():
            btn.setChecked(name == key)
        self._apply_filter()

    def _apply_filter(self) -> None:
        query = (self.search_edit.text() or "").strip().lower()
        for item in self._items.values():
            item.setHidden(False)

        def item_matches(item: QTreeWidgetItem) -> bool:
            node_id = str(item.data(0, Qt.ItemDataRole.UserRole) or "")
            events = self._node_events.get(node_id) or []
            text = self._search_text.get(node_id, "")
            kind_ok = self._filter_kind_matches(events)
            item_text = f"{item.text(0)} {item.text(1)}".lower()
            query_ok = not query or query in text or query in item_text
            child_ok = False
            for idx in range(item.childCount()):
                child_ok = item_matches(item.child(idx)) or child_ok
            visible = (kind_ok and query_ok) or child_ok
            item.setHidden(not visible)
            return visible

        for idx in range(self.tree.topLevelItemCount()):
            item_matches(self.tree.topLevelItem(idx))

    def _filter_kind_matches(self, events: list[dict[str, Any]]) -> bool:
        if self._active_filter == "all":
            return True
        if self._active_filter == "error":
            return any(str(event.get("status") or "").lower() in {"error", "failed"} or str(event.get("phase") or "") == "error" for event in events)
        return any(str(event.get("kind") or "").lower() == self._active_filter for event in events)

    def _on_selection_changed(self) -> None:
        items = self.tree.selectedItems()
        if not items:
            return
        node_id = str(items[0].data(0, Qt.ItemDataRole.UserRole) or "")
        self._show_node(node_id)

    def _show_node(self, node_id: str) -> None:
        if self._detail_job is not None:
            self._detail_job.abandon()
            self._detail_job = None
        self._detail = None
        self.retest_btn.hide()
        self.copy_call_btn.hide()
        events = self._node_events.get(node_id) or []
        if not events:
            self.node_title.setText("暂无运行记录")
            for label in (self.status_value, self.duration_value, self.token_value, self.ref_value):
                label.setText("—")
            self.overview_text.setPlainText("暂无事件")
            self.request_text.setPlainText("未保存 payload")
            self.raw_result_text.setPlainText("暂无原始结果")
            self.model_result_text.setPlainText("暂无模型视图")
            self.event_text.setPlainText("暂无事件")
            return
        self.node_title.setText(self._node_title(node_id))
        first = events[0]
        last = events[-1]
        data = dict(last.get("data") or {})
        refs = self._merged_refs(events)
        self.status_value.setText(str(last.get("status") or first.get("status") or "-"))
        self.duration_value.setText(self._format_duration(last.get("duration_ms")))
        tokens = next(
            (
                dict(event.get("data") or {}).get("tokens")
                for event in reversed(events)
                if dict(event.get("data") or {}).get("tokens") not in (None, "")
            ),
            "-",
        )
        self.token_value.setText(str(tokens))
        self.ref_value.setText(" / ".join(refs.keys()) if refs else "-")

        overview = {
            "node_id": first.get("node_id") or node_id,
            "request_id": first.get("request_id") or "",
            "parent_id": first.get("parent_id") or "",
            "kind": first.get("kind") or "",
            "name": first.get("name") or last.get("name") or "",
            "status": last.get("status") or "",
            "turn": first.get("turn") or 0,
            "tool_call_id": last.get("tool_call_id") or "",
            "tool_name": last.get("tool_name") or "",
            "subtask_id": last.get("subtask_id") or "",
            "refs": refs,
            "summary": last.get("summary") or first.get("summary") or "",
            "data": data,
        }
        self.overview_text.setPlainText(_pretty_json(overview))
        kind = str(first.get("kind") or last.get("kind") or "")
        self.tabs.setTabText(1, "请求参数" if kind == "tool" else "请求")
        self.tabs.setTabText(2, "原始结果" if kind == "tool" else "原始响应")
        self.tabs.setTabText(3, "模型视图" if kind == "tool" else "响应")
        for view in (self.request_text, self.raw_result_text, self.model_result_text):
            view.setPlainText("正在读取节点明细…")
        self.event_text.setPlainText(_pretty_json(events))
        services, cid = self._services, self._conversation.id
        selected = [dict(event) for event in events]
        if services is not None:
            operation = lambda: services.conv_service.trace_node(
                cid, first.get("node_id", ""), run_id=first.get("request_id", ""), events=selected)
        else:
            operation = lambda: read_trace_node(self._debug_dir, selected, max_bytes=self.MAX_PAYLOAD_BYTES)
        job = BackgroundJob(operation)
        self._detail_job = job
        self.destroyed.connect(job.abandon)
        job.signals.finished.connect(lambda result, error: self._finish_detail(job, result, error))
        QThreadPool.globalInstance().start(job)

    def _finish_detail(self, job, result, error):
        if self._detail_job is not job:
            return
        self._detail_job = None
        if error:
            for view in (self.request_text, self.raw_result_text, self.model_result_text):
                view.setPlainText(f"节点明细读取失败：{error}")
            return
        self._detail = result
        for view, key in ((self.request_text, "request"), (self.raw_result_text, "raw_result"),
                          (self.model_result_text, "model_result")):
            view.setPlainText(self._part_text(result[key]))
        tool = result.get("tool")
        ordinary = bool(tool and not tool["name"].startswith(("agent__", "user__", "context__")))
        self.retest_btn.setVisible(ordinary)
        self.copy_call_btn.setVisible(ordinary)
        busy = self._services is not None and self._services.conv_service.is_active(self._conversation.id)
        self.retest_btn.setEnabled(ordinary and self._services is not None and not busy)
        self.retest_btn.setToolTip("目标会话正在运行，请结束后刷新。" if busy else "使用当前环境和权限执行一次新调用。")
        self.copy_call_btn.setEnabled(ordinary and tool.get("arguments_status") == "ok")
        self.copy_call_btn.setToolTip("复制调用代码" if ordinary and tool.get("arguments_status") == "ok"
                                     else "参数不完整，请在复测窗口补齐。")

    @staticmethod
    def _part_text(part):
        source = {"trace": "Trace 捕获", "archive": "Archive 原文", "event": "事件预览"}.get(part["source"], part["source"])
        status = {"ok": "已读取（脱敏诊断副本）", "partial": "不完整", "missing": "记录缺失",
                  "not_captured": "未捕获", "too_large": "超过预览上限", "invalid_path": "引用路径无效"}.get(part["status"], part["status"])
        content = part.get("content")
        text = content if isinstance(content, str) else _pretty_json(content) if content is not None else ""
        return f"来源：{source} · {status}\n{part.get('note', '')}\n\n{text}".strip()

    def _copy_tool_call(self):
        tool = (self._detail or {}).get("tool")
        if tool and tool["arguments_status"] == "ok":
            QApplication.clipboard().setText(tool_call_code(tool["name"], tool["arguments"], self._conversation.id))

    def _open_retest(self):
        tool = (self._detail or {}).get("tool")
        if not tool or self._services is None:
            return
        dialog = ToolRetestDialog(self._conversation.id, tool, self.parentWidget(), services=self._services,
                                  on_finished=self._on_tool_finished)
        dialog.show()
        self._retest_dialog = dialog

    def _show_sdk(self):
        detail = self._detail or {}
        self._sdk_dialog = show_sdk_dialog(self, self._conversation.id, detail.get("run_id", ""), detail.get("node_id", ""))

    @staticmethod
    def _format_duration(value: Any) -> str:
        try:
            ms = int(value or 0)
        except Exception:
            ms = 0
        if ms <= 0:
            return "-"
        if ms < 1000:
            return f"{ms} ms"
        return f"{ms / 1000:.2f} s"

    @staticmethod
    def _merged_refs(events: list[dict[str, Any]]) -> dict[str, str]:
        refs: dict[str, str] = {}
        for event in events:
            current = event.get("refs") if isinstance(event.get("refs"), dict) else {}
            for key, value in current.items():
                if str(value or "").strip():
                    refs[str(key)] = str(value)
        return refs

    def _copy_request_id(self) -> None:
        request_id = ""
        items = self.tree.selectedItems()
        if items:
            node_id = str(items[0].data(0, Qt.ItemDataRole.UserRole) or "")
            for event in self._node_events.get(node_id) or []:
                request_id = str(event.get("request_id") or "")
                if request_id:
                    break
        for event in self._events:
            if request_id:
                break
            request_id = str(event.get("request_id") or "")
        if request_id:
            QApplication.clipboard().setText(request_id)

    def _copy_current_tab(self) -> None:
        widget = self.tabs.currentWidget()
        if isinstance(widget, QPlainTextEdit):
            QApplication.clipboard().setText(widget.toPlainText())

    def _open_debug_dir(self) -> None:
        self._debug_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._debug_dir)))
