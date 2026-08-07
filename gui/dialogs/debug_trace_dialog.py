from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QSize, Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QFontDatabase
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QSplitter,
    QTabWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from models.conversation import Conversation
from core.observability import resolve_debug_trace_dir
from core.observability.debug_trace import redact_debug_payload
from core.content.archive_store import SessionArchiveStore
from gui.utils.icon_manager import Icons
from gui.utils.window_geometry import apply_workbench_dialog_size
from gui.widgets.themed_line_edit import ThemedLineEdit, ThemedPlainTextEdit, ThemedSelectableLabel


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

    def __init__(self, conversation: Conversation, parent=None):
        super().__init__(parent)
        self.setWindowTitle("调用链路")
        self.setObjectName("debug_trace_dialog")
        self.setModal(False)
        apply_workbench_dialog_size(self)

        self._conversation = conversation
        self._debug_dir = self._resolve_debug_dir(conversation)
        self._archive_store = SessionArchiveStore(
            str(getattr(conversation, "work_dir", "") or "."),
            conversation_id=str(getattr(conversation, "id", "") or "default"),
        )
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
            work_dir=str(getattr(conversation, "work_dir", "") or "."),
        )

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        header = QHBoxLayout()
        self.subtitle = ThemedSelectableLabel("")
        self.subtitle.setProperty("muted", True)
        self.subtitle.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
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
        root.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)
        left_layout.setSpacing(8)

        self.search_edit = ThemedLineEdit()
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

        self.overview_cards = QHBoxLayout()
        self.overview_cards.setSpacing(8)
        self.status_card = self._stat_card("状态")
        self.duration_card = self._stat_card("耗时")
        self.token_card = self._stat_card("Token")
        self.ref_card = self._stat_card("引用")
        for card in (self.status_card, self.duration_card, self.token_card, self.ref_card):
            self.overview_cards.addWidget(card)
        right_layout.addLayout(self.overview_cards)

        self.tabs = QTabWidget()
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
        splitter.setSizes([320, 780])

        self.path_label = ThemedSelectableLabel("")
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
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return btn

    def _stat_card(self, label: str) -> QFrame:
        card = QFrame()
        card.setObjectName("stat_card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)
        label_widget = QLabel(label)
        label_widget.setObjectName("stat_label")
        value_widget = ThemedSelectableLabel("-")
        value_widget.setObjectName("stat_value")
        value_widget.setWordWrap(True)
        value_widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(label_widget)
        layout.addWidget(value_widget)
        card._value_label = value_widget  # type: ignore[attr-defined]
        return card

    @staticmethod
    def _set_card(card: QFrame, value: str) -> None:
        label = getattr(card, "_value_label", None)
        if isinstance(label, QLabel):
            label.setText(str(value or "-"))

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
        self._events = self._read_events()
        self._update_context_labels()
        self._rebuild_tree()
        self._apply_filter()
        if self.tree.topLevelItemCount() > 0 and not self.tree.selectedItems():
            first = self.tree.topLevelItem(0)
            self.tree.setCurrentItem(first)

    def _update_context_labels(self) -> None:
        session_id = str(getattr(self._conversation, "id", "") or "-")
        request_ids = []
        seen = set()
        for event in self._events:
            request_id = str(event.get("request_id") or "").strip()
            if request_id and request_id not in seen:
                seen.add(request_id)
                request_ids.append(request_id)
        if len(request_ids) == 1:
            request_text = request_ids[0]
        elif request_ids:
            request_text = f"{len(request_ids)} 个 request"
        else:
            request_text = "-"
        self.subtitle.setText(
            f"session: {_short_id(session_id)}    request: {_short_id(request_text)}    events: {len(self._events)}"
        )
        self.subtitle.setToolTip(
            f"session: {session_id}\nrequest: {', '.join(request_ids) or '-'}"
        )
        compact_path = self._compact_debug_path(session_id)
        self.path_label.setText(f"debug: {compact_path}")
        self.path_label.setToolTip(str(self._debug_dir))

    def _compact_debug_path(self, session_id: str) -> str:
        name = _short_id(session_id)
        return str(Path(".pycat") / "sessions" / name / "debug")

    def _read_events(self) -> list[dict[str, Any]]:
        path = self._debug_dir / "events.jsonl"
        events: list[dict[str, Any]] = []
        if not path.exists():
            return events
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except Exception:
                    continue
                if isinstance(payload, dict):
                    events.append(payload)
        except Exception:
            return events
        return events

    def _rebuild_tree(self) -> None:
        self.tree.clear()
        self._node_events = {}
        self._items = {}
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
            empty = QTreeWidgetItem(["暂无 trace 事件", ""])
            empty.setData(0, Qt.ItemDataRole.UserRole, "")
            self.tree.addTopLevelItem(empty)
            return

        for node_id in order:
            events = self._node_events.get(node_id) or []
            item = QTreeWidgetItem([self._node_title(node_id), self._node_time_label(events)])
            item.setData(0, Qt.ItemDataRole.UserRole, node_id)
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            tooltip = self._node_time_tooltip(events)
            if tooltip:
                item.setToolTip(0, tooltip)
                item.setToolTip(1, tooltip)
            self._items[node_id] = item

        for node_id in order:
            item = self._items[node_id]
            parent_id = parent_by_node.get(node_id, "")
            parent = self._items.get(parent_id)
            if parent is not None and parent is not item:
                parent.addChild(item)
            else:
                self.tree.addTopLevelItem(item)
        self.tree.expandAll()

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
            text = " ".join(_pretty_json(event).lower() for event in events)
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
        events = self._node_events.get(node_id) or []
        if not events:
            self.overview_text.setPlainText("暂无事件")
            self.request_text.setPlainText("未保存 payload")
            self.raw_result_text.setPlainText("暂无原始结果")
            self.model_result_text.setPlainText("暂无模型视图")
            self.event_text.setPlainText("暂无事件")
            return
        first = events[0]
        last = events[-1]
        data = dict(last.get("data") or {})
        refs = self._merged_refs(events)
        self._set_card(self.status_card, str(last.get("status") or first.get("status") or "-"))
        self._set_card(self.duration_card, self._format_duration(last.get("duration_ms")))
        tokens = next(
            (
                dict(event.get("data") or {}).get("tokens")
                for event in reversed(events)
                if dict(event.get("data") or {}).get("tokens") not in (None, "")
            ),
            "-",
        )
        self._set_card(self.token_card, str(tokens))
        self._set_card(self.ref_card, " / ".join(refs.keys()) if refs else "-")

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
        response_payload, response_state = self._load_json_ref(refs.get("response"))
        content_id = self._content_id(events, response_payload)
        request_text = self._tool_request_text(
            events,
            refs=refs,
            content_id=content_id,
        )
        self.request_text.setPlainText(request_text)
        if kind == "tool":
            self.raw_result_text.setPlainText(
                self._tool_raw_result_text(
                    response_payload,
                    response_state=response_state,
                    content_id=content_id,
                )
            )
            self.model_result_text.setPlainText(
                self._tool_model_result_text(
                    events,
                    response_payload=response_payload,
                    response_state=response_state,
                )
            )
        else:
            response = self._load_ref(refs.get("response"), empty="未保存完整响应 payload")
            self.raw_result_text.setPlainText(response)
            self.model_result_text.setPlainText(response)
        self.event_text.setPlainText(_pretty_json(events))

    def _tool_request_text(
        self,
        events: list[dict[str, Any]],
        *,
        refs: dict[str, str],
        content_id: str,
    ) -> str:
        request_payload, state = self._load_json_ref(refs.get("request"))
        if request_payload is not None and not self._contains_truncation(request_payload):
            return self._labeled_payload("Trace request attachment", "exact", request_payload)
        archived_input = self._archive_input(content_id)
        if archived_input is not None:
            reason = "attachment missing" if request_payload is None else "legacy attachment truncated"
            return self._labeled_payload(f"Archive input ({reason})", "exact", archived_input)
        fallback = self._request_fallback(events)
        if request_payload is not None:
            return self._labeled_payload("Legacy Trace request attachment", "partial", request_payload)
        if fallback:
            return self._labeled_payload("Bounded start event", "derived", fallback)
        return f"source: {state or 'missing'}\nexactness: unavailable\n\n未保存请求参数，Archive 中也没有可恢复 input。"

    def _tool_raw_result_text(
        self,
        response_payload: Any,
        *,
        response_state: str,
        content_id: str,
    ) -> str:
        raw_result = response_payload.get("raw_result") if isinstance(response_payload, dict) else None
        raw_content = raw_result.get("content") if isinstance(raw_result, dict) else None
        if raw_content is not None and not self._contains_truncation(raw_content):
            return self._labeled_content("Trace response attachment", "exact", raw_content)
        archived = self._archive_original(content_id)
        if archived is not None:
            reason = "attachment missing" if raw_content is None else "legacy attachment truncated"
            return self._labeled_content(f"Archive original ({reason})", "exact", archived)
        if raw_content is not None:
            return self._labeled_content("Legacy Trace response attachment", "partial", raw_content)
        detail = response_state or "response attachment missing"
        return (
            f"source: {detail}\nexactness: unavailable\ncontent_id: {content_id or '-'}\n\n"
            "未保存原始结果，且 Archive 原文不存在。"
        )

    def _tool_model_result_text(
        self,
        events: list[dict[str, Any]],
        *,
        response_payload: Any,
        response_state: str,
    ) -> str:
        model_result = response_payload.get("model_result") if isinstance(response_payload, dict) else None
        content = model_result.get("content") if isinstance(model_result, dict) else None
        if content is not None:
            return self._labeled_content("Trace response attachment", "derived", content)
        fallback = self._response_fallback(events)
        if fallback:
            return self._labeled_payload("Bounded end event", "derived", fallback)
        return f"source: {response_state or 'missing'}\nexactness: unavailable\n\n未保存模型实际收到的结果。"

    def _archive_original(self, content_id: str) -> str | None:
        if not content_id:
            return None
        try:
            record = self._archive_store.read_record(content_id)
            if record is None:
                return None
            return str(redact_debug_payload(self._archive_store.read_original(record)))
        except Exception:
            return None

    def _archive_input(self, content_id: str) -> Any:
        if not content_id:
            return None
        try:
            value = self._archive_store.read_input(content_id)
            return redact_debug_payload(value) if value is not None else None
        except Exception:
            return None

    @staticmethod
    def _content_id(events: list[dict[str, Any]], response_payload: Any) -> str:
        if isinstance(response_payload, dict) and isinstance(response_payload.get("archive"), dict):
            value = str(response_payload["archive"].get("content_id") or "").strip()
            if value:
                return value
        for event in reversed(events):
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            value = str(data.get("content_id") or "").strip()
            if value:
                return value
            refs = event.get("refs") if isinstance(event.get("refs"), dict) else {}
            value = str(refs.get("content_id") or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _contains_truncation(value: Any) -> bool:
        if isinstance(value, dict):
            if value.get("truncated") is True and "preview" in value:
                return True
            return any(DebugTraceDialog._contains_truncation(item) for item in value.values())
        if isinstance(value, list):
            return any(DebugTraceDialog._contains_truncation(item) for item in value)
        return False

    @staticmethod
    def _content_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif isinstance(item, dict) and item.get("type") in {"image", "image_ref"}:
                    parts.append(_pretty_json(item))
                else:
                    parts.append(_pretty_json(item) if isinstance(item, (dict, list)) else str(item))
            return "\n".join(parts)
        return _pretty_json(value) if isinstance(value, (dict, list)) else str(value or "")

    @classmethod
    def _labeled_content(cls, source: str, exactness: str, value: Any) -> str:
        text = cls._content_text(value)
        return f"source: {source}\nexactness: {exactness}\ncharacters: {len(text)}\n\n{text}"

    @staticmethod
    def _labeled_payload(source: str, exactness: str, value: Any) -> str:
        text = _pretty_json(value)
        return f"source: {source}\nexactness: {exactness}\ncharacters: {len(text)}\n\n{text}"

    @staticmethod
    def _request_fallback(events: list[dict[str, Any]]) -> dict[str, Any]:
        for event in events:
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            args = data.get("args") if isinstance(data, dict) else None
            if args is not None:
                return {
                    "tool_name": event.get("tool_name") or event.get("name") or "",
                    "tool_call_id": event.get("tool_call_id") or "",
                    "allowed": data.get("allowed"),
                    "arguments": args,
                }
        return {}

    @staticmethod
    def _response_fallback(events: list[dict[str, Any]]) -> dict[str, Any]:
        for event in reversed(events):
            if str(event.get("phase") or "") not in {"end", "error", "repetition"}:
                continue
            return {
                "tool_name": event.get("tool_name") or event.get("name") or "",
                "tool_call_id": event.get("tool_call_id") or "",
                "status": event.get("status") or "",
                "summary": event.get("summary") or "",
                "data": event.get("data") if isinstance(event.get("data"), dict) else {},
            }
        return {}

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

    def _load_ref(self, relative: str | None, *, empty: str = "未保存 payload") -> str:
        rel = str(relative or "").strip()
        if not rel:
            return empty
        target = (self._debug_dir / rel).resolve()
        try:
            target.relative_to(self._debug_dir.resolve())
        except ValueError:
            return "引用路径无效"
        if not target.exists():
            return "引用文件不存在"
        try:
            raw = target.read_text(encoding="utf-8")
            try:
                return _pretty_json(json.loads(raw))
            except Exception:
                return raw
        except Exception as exc:
            return f"读取失败: {exc}"

    def _load_json_ref(self, relative: str | None) -> tuple[Any, str]:
        rel = str(relative or "").strip()
        if not rel:
            return None, "attachment not recorded"
        target = (self._debug_dir / rel).resolve()
        try:
            target.relative_to(self._debug_dir.resolve())
        except ValueError:
            return None, "invalid attachment path"
        if not target.is_file():
            return None, "attachment file missing"
        try:
            return json.loads(target.read_text(encoding="utf-8")), ""
        except Exception as exc:
            return None, f"attachment parse failed: {exc}"

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
