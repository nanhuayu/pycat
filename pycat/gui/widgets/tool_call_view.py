"""Tool call, thinking, and subagent trace views used inside chat messages."""

from __future__ import annotations

import json
import logging
import math
import os
import re
from typing import Any, Callable, Iterable, List

from PyQt6.QtCore import QCoreApplication, QRect, QSize, Qt, QTimer
from PyQt6.QtGui import QPainter, QTextOption
from PyQt6.QtWidgets import (
    QFrame,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.theme import COMPACT_CONTROL_HEIGHT
from pycat.gui.view_models.message_tree import ToolInvocationView, build_message_tree_view_model
from pycat.gui.view_models.tooling_labels import tool_name_label
from pycat.gui.widgets.markdown_view import MarkdownView
from pycat.gui.widgets.themed_line_edit import ThemedContextMenuMixin
from pycat.gui.widgets.workflow_capsule import WorkflowCapsuleRow, create_artifact_capsule
from pycat.models.contracts.content import ContentRef, FileChange
from pycat.models.conversation import (
    normalize_subtask_run,
    normalize_tool_result,
    tool_call_kind,
    tool_call_name,
)

logger = logging.getLogger(__name__)

EDIT_TOOL_NAMES = {"file__write", "file__edit", "file__patch", "file__delete"}


def _tool_call_display_name(name: str) -> str:
    return tool_name_label(str(name or 'unknown_tool'))


def _coerce_tool_arguments(tool_call: dict | None) -> dict[str, Any]:
    data = tool_call or {}
    func = data.get('function', {}) if isinstance(data.get('function'), dict) else {}
    raw = func.get('arguments', data.get('arguments', {})) if isinstance(func, dict) else data.get('arguments', {})
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str):
        return {}
    try:
        payload = json.loads(raw)
        return dict(payload) if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _file_name_for_display(path: str) -> str:
    normalized = str(path or "").replace("\\", "/").rstrip("/")
    if not normalized:
        return QCoreApplication.translate('ToolCallView', '未命名文件')
    return os.path.basename(normalized) or normalized


def _file_change_action(name: str) -> tuple[str, str]:
    if name == "file__write":
        return "write", QCoreApplication.translate('ToolCallView', '已写入')
    if name == "file__delete":
        return "delete", QCoreApplication.translate('ToolCallView', '已删除')
    if name == "file__patch":
        return "patch", QCoreApplication.translate('ToolCallView', '已应用补丁')
    return "edit", QCoreApplication.translate('ToolCallView', '已编辑')


def _file_change_meta(name: str, args: dict[str, Any], result: dict[str, Any] | None) -> str:
    parts: list[str] = []
    content = ""
    if isinstance(result, dict):
        content = str(result.get("content") or result.get("summary") or "")
    diff = str(args.get("diff") or "")
    old_str = str(args.get("old_str") or "")
    new_str = str(args.get("new_str") or "")
    if name == "file__patch":
        hunk_count = len(re.findall(r"(?m)^@@\s", diff))
        if hunk_count:
            parts.append(QCoreApplication.translate('ToolCallView', '{hunk_count} 个 hunk').format(hunk_count=hunk_count))
    elif name == "file__write":
        written_chars = len(str(args.get("content") or ""))
        if written_chars:
            parts.append(QCoreApplication.translate('ToolCallView', '{written_chars} 字符').format(written_chars=written_chars))
    elif name == "file__delete":
        if args.get("recursive"):
            parts.append(QCoreApplication.translate('ToolCallView', '递归'))
    elif name == "file__edit":
        if "Regex match" in content:
            parts.append(QCoreApplication.translate('ToolCallView', '正则匹配'))
        elif "Exact match" in content:
            parts.append(QCoreApplication.translate('ToolCallView', '精确匹配'))
        elif diff:
            parts.append(QCoreApplication.translate('ToolCallView', '补丁模式'))
        elif old_str or new_str:
            parts.append(QCoreApplication.translate('ToolCallView', '文本替换'))
    return " · ".join(parts)


def _tool_call_kind_label(kind: str) -> str:
    return {
        'subagent': QCoreApplication.translate('ToolCallView', '子 Agent'),
        'capability': QCoreApplication.translate('ToolCallView', '能力'),
        'tool': QCoreApplication.translate('ToolCallView', '工具'),
    }.get(kind, QCoreApplication.translate('ToolCallView', '工具'))


def _plain_summary(text: Any, limit: int = 160) -> str:
    value = str(text or '').strip().replace('\r\n', '\n').replace('\r', '\n')
    value = re.sub(r"\s+", " ", value)
    if len(value) > limit:
        return value[: max(0, limit - 1)].rstrip() + '…'
    return value


def _fit_text_browser_height(view: QTextBrowser, *, min_height: int = 18, max_height: int = 120) -> None:
    """Fit a QTextBrowser to its document height within a compact bound."""
    try:
        width = view.viewport().width() or view.width() or 360
        view.document().setTextWidth(max(120, int(width)))
        height = int(math.ceil(view.document().documentLayout().documentSize().height())) + 2
        view.setFixedHeight(max(min_height, min(max_height, height)))
    except Exception as exc:
        logger.debug("Failed to fit text browser height: %s", exc)


class CompactTextBrowser(ThemedContextMenuMixin, QTextBrowser):
    """Read-only text browser that keeps height close to its content."""

    def __init__(self, text: str = "", parent=None, *, min_height: int = 18, max_height: int = 96):
        super().__init__(parent)
        self._min_height = min_height
        self._max_height = max_height
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.document().setDocumentMargin(2)
        opt = self.document().defaultTextOption()
        opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.document().setDefaultTextOption(opt)
        try:
            self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        except Exception:
            pass
        self.setPlainText(text)
        self.refit_height()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refit_height()

    def refit_height(self) -> None:
        _fit_text_browser_height(self, min_height=self._min_height, max_height=self._max_height)


class ToolDetailPanel(QWidget):
    """Shared details panel for tool arguments and results."""

    def __init__(
        self,
        tool_call: dict | None,
        result_payload: dict[str, Any] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.tool_call = tool_call if isinstance(tool_call, dict) else {}
        self.result_payload = normalize_tool_result(result_payload) if result_payload is not None else None
        self.args_view: CompactTextBrowser | None = None
        self.result_label: QLabel | None = None
        self.result_view: MarkdownView | None = None
        self._setup_ui()
        if self.result_payload is not None:
            self.set_result(self.result_payload)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        args_label = QLabel(QCoreApplication.translate('ToolCallView', '输入参数'))
        args_label.setObjectName("tool_detail_label")
        layout.addWidget(args_label)

        self.args_view = CompactTextBrowser(self._format_arguments(), min_height=44, max_height=112)
        self.args_view.setObjectName("tool_args_view")
        layout.addWidget(self.args_view)

        self.result_label = QLabel(QCoreApplication.translate('ToolCallView', '执行结果'))
        self.result_label.setObjectName("tool_detail_label")
        self.result_label.setVisible(False)
        layout.addWidget(self.result_label)

        self.result_view = MarkdownView("")
        self.result_view.setObjectName("tool_result_view")
        self.result_view.document().setDocumentMargin(7)
        self.result_view.set_height_adjustment(minimum_height=56, padding=4, maximum_height=220)
        self.result_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.result_view.setVisible(False)
        layout.addWidget(self.result_view)

    def _format_arguments(self) -> str:
        func = self.tool_call.get('function', {}) if isinstance(self.tool_call, dict) else {}
        raw = func.get('arguments', '{}') if isinstance(func, dict) else '{}'
        if isinstance(raw, dict):
            return json.dumps(raw, indent=2, ensure_ascii=False)
        if not isinstance(raw, str):
            return str(raw or '{}')
        try:
            obj = json.loads(raw)
            return json.dumps(obj, indent=2, ensure_ascii=False)
        except Exception:
            return raw

    def set_result(self, result_payload: dict[str, Any]) -> None:
        self.result_payload = normalize_tool_result(result_payload)
        if self.result_label is not None:
            self.result_label.setVisible(True)
        if self.result_view is not None:
            self.result_view.setVisible(True)
            self.result_view.set_markdown(str(self.result_payload.get('content') or ''))

    def refit_height(self) -> None:
        if self.args_view is not None:
            self.args_view.refit_height()
            QTimer.singleShot(0, self.args_view.refit_height)
        if self.result_view is not None and self.result_view.isVisible():
            self.result_view.refit_height()
            QTimer.singleShot(0, self.result_view.refit_height)


class DisclosureButton(QPushButton):
    """Single-line, keyboard accessible disclosure with a reserved chevron."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._full_text = str(text or "")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(COMPACT_CONTROL_HEIGHT)
        self.setMinimumWidth(0)
        self.setIconSize(QSize(18, 18))
        self.set_expanded(False)

    def set_expanded(self, expanded: bool) -> None:
        self.setChecked(expanded)
        if self.property('expanded') != expanded:
            self.setProperty('expanded', expanded)
            self.style().unpolish(self)
            self.style().polish(self)
        self.update()

    def setText(self, text: str) -> None:
        self._full_text = str(text or "")
        super().setText(self._full_text)
        self.setAccessibleName(self._full_text)
        self.update()

    def fullText(self) -> str:
        return self._full_text

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        return QSize(min(hint.width(), 220), hint.height())

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        return QSize(0, hint.height())

    def paintEvent(self, event) -> None:
        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        metrics = self.fontMetrics()
        reserve = 34
        if self.icon() and not self.icon().isNull():
            reserve += self.iconSize().width() + 4
        opt.text = metrics.elidedText(self._full_text, Qt.TextElideMode.ElideRight, max(12, self.width() - reserve))
        painter = QPainter(self)
        self.style().drawControl(QStyle.ControlElement.CE_PushButton, opt, painter, self)
        arrow = Icons.CHEVRON_DOWN if self.isChecked() else Icons.CHEVRON_RIGHT
        Icons.get_muted(arrow, scale_factor=1.0).paint(
            painter, QRect(self.width() - 22, (self.height() - 14) // 2, 14, 14)
        )


class ThinkingSection(QWidget):
    """Collapsible thinking section - Concise Style"""

    def __init__(self, thinking_content: str, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.thinking_content = thinking_content
        self._rendered_content = ''
        self.is_expanded = False
        self.content_widget: MarkdownView | None = None
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 1, 0, 1)
        layout.setSpacing(0)

        self.toggle_btn = DisclosureButton()
        self.toggle_btn.setObjectName("thinking_toggle")
        self.toggle_btn.setIcon(Icons.get_muted(Icons.THINKING, scale_factor=1.0))
        self.toggle_btn.setText(QCoreApplication.translate('ToolCallView', '思考过程'))

        self.toggle_btn.clicked.connect(self._toggle)
        layout.addWidget(self.toggle_btn)

    def _ensure_content_widget(self) -> MarkdownView:
        if self.content_widget is None:
            self.content_widget = MarkdownView('')
            self.content_widget.setObjectName("thinking_content")
            self.content_widget.document().setDocumentMargin(7)
            self.content_widget.set_height_adjustment(minimum_height=40, padding=4, maximum_height=180)
            self.layout().addWidget(self.content_widget)
        return self.content_widget

    def set_content(self, content: str) -> None:
        self.thinking_content = str(content or '')
        if self.is_expanded and self._rendered_content != self.thinking_content:
            self._ensure_content_widget().set_markdown(self.thinking_content)
            self._rendered_content = self.thinking_content

    def _toggle(self):
        self.set_expanded(not self.is_expanded)

    def set_expanded(self, expanded: bool) -> None:
        self.is_expanded = bool(expanded)
        if self.content_widget is not None:
            self.content_widget.setVisible(self.is_expanded)
        self.toggle_btn.set_expanded(self.is_expanded)
        if self.is_expanded:
            content_widget = self._ensure_content_widget()
            self.set_content(self.thinking_content)
            content_widget.setVisible(True)
            content_widget.refit_height()
            QTimer.singleShot(0, content_widget.refit_height)


class ToolCallItem(QWidget):
    """Widget for a single tool call with collapsible details - Concise Style"""

    def __init__(
        self,
        tool_call: dict | ToolInvocationView,
        parent=None,
        *,
        work_dir: str = "",
        artifact_lookup: Callable[[str], object | None] | None = None,
        embedded_message_factory: Callable[..., object] | None = None,
    ):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.work_dir = str(work_dir or "")
        self.artifact_lookup = artifact_lookup
        self._embedded_message_factory = embedded_message_factory
        self.invocation = tool_call if isinstance(tool_call, ToolInvocationView) else None
        self.tool_call = self.invocation.tool_call if self.invocation is not None else tool_call
        self.result_payload = dict(self.invocation.result) if self.invocation is not None and isinstance(self.invocation.result, dict) else None
        if self.result_payload is None and isinstance(self.tool_call, dict) and 'result' in self.tool_call:
            self.result_payload = normalize_tool_result(self.tool_call.get('result'))
        self.tool_id = self.invocation.id if self.invocation is not None else self.tool_call.get('id')
        self.is_expanded = False
        self.subtask_widget = None
        self.toggle_btn = None
        self.summary_label = None
        self.meta_label = None
        self.file_change_widget = None
        self.file_change_title = None
        self.file_change_meta = None
        self.file_change_icon = None
        self.artifact_widgets: list[WorkflowCapsuleRow] = []
        self.capsule_container = None
        self.capsule_layout = None
        self.details_widget = None
        self.details_layout = None
        self.detail_panel = None
        self.result_label = None
        self.result_view = None
        self.args_view = None
        self._subtask_trace: dict[str, Any] | None = None
        self._setup_ui()

    def set_work_dir(self, work_dir: str) -> None:
        self.work_dir = str(work_dir or "")
        if isinstance(self.file_change_widget, WorkflowCapsuleRow):
            self.file_change_widget.set_work_dir(self.work_dir)
            self._refresh_file_change_summary()
        if self.subtask_widget is not None and hasattr(self.subtask_widget, "set_work_dir"):
            self.subtask_widget.set_work_dir(self.work_dir)
        for row in self.artifact_widgets:
            row.set_work_dir(self.work_dir)

    def _setup_capsule_container(self, parent_layout: QVBoxLayout) -> None:
        self.capsule_container = QWidget()
        self.capsule_container.setObjectName("tool_capsule_container")
        self.capsule_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.capsule_layout = QVBoxLayout(self.capsule_container)
        self.capsule_layout.setContentsMargins(0, 0, 0, 0)
        self.capsule_layout.setSpacing(2)
        parent_layout.addWidget(self.capsule_container)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 1, 0, 1)
        layout.setSpacing(2)

        name = self._tool_name()
        kind = self._tool_kind(name)

        result_payload = self.result_payload or normalize_tool_result('')

        # Header (Toggle button)
        self.toggle_btn = DisclosureButton()
        self.toggle_btn.setObjectName("tool_call_header")
        self.toggle_btn.setProperty("kind", kind)
        self.toggle_btn.setProperty("status", "running")
        self.toggle_btn.setIcon(self._header_icon(kind, name))
        self.toggle_btn.setToolTip(self._header_tooltip())

        # Initial state is "Running"
        self.toggle_btn.setText(self._running_title(name))
        self.toggle_btn.clicked.connect(self._toggle)
        layout.addWidget(self.toggle_btn)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(False)
        self.summary_label.setProperty("muted", True)
        self.summary_label.setObjectName("tool_call_summary")
        self.summary_label.setVisible(False)
        layout.addWidget(self.summary_label)

        self.meta_label = QLabel("")
        self.meta_label.setWordWrap(True)
        self.meta_label.setProperty("muted", True)
        self.meta_label.setVisible(False)
        layout.addWidget(self.meta_label)

        self._setup_capsule_container(layout)
        self._add_file_change_summary(self.capsule_layout)

        if self.result_payload is not None:
            if result_payload.get('type') == 'subtask_run':
                trace = result_payload.get('run') if isinstance(result_payload.get('run'), dict) else None
                self.set_subtask(trace if isinstance(trace, dict) else self._placeholder_subtask_trace(result_payload))
                return
            self._apply_result_header(self.result_payload)
        self._refresh_artifact_capsules()

    def set_subtask(self, trace: dict):
        if not isinstance(trace, dict):
            return
        current = self.result_payload or normalize_tool_result('')
        current_metadata = dict(current.get('metadata') or {})
        trace_metadata = trace.get('metadata') if isinstance(trace.get('metadata'), dict) else {}
        current_metadata.update({str(k): v for k, v in dict(trace_metadata or {}).items() if v not in (None, "", [])})
        result_payload = {
            'type': 'subtask_run',
            'content': str(trace.get('final_message') or trace.get('error') or trace.get('goal') or current.get('content') or ''),
            'summary': str(trace.get('final_message') or trace.get('error') or trace.get('goal') or current.get('summary') or '')[:220],
            'metadata': current_metadata,
            'run': trace,
        }
        self.result_payload = result_payload
        self._subtask_trace = trace
        if self.subtask_widget is not None:
            self.subtask_widget.set_trace(trace)
        else:
            self._ensure_subtask_widget()
        if self.detail_panel is not None:
            self.detail_panel.set_result(result_payload)
            self._sync_detail_aliases()
        self._apply_result_header(result_payload)
        self._refresh_artifact_capsules()

    def _ensure_subtask_widget(self) -> "SubtaskRunWidget | None":
        if self._subtask_trace is None:
            return None
        if self.subtask_widget is None:
            self.subtask_widget = SubtaskRunWidget(
                self._subtask_trace,
                work_dir=self.work_dir,
                embedded_message_factory=self._embedded_message_factory,
            )
            capsule_layout = self.capsule_layout
            if capsule_layout is None:
                return self.subtask_widget
            capsule_layout.addWidget(self.subtask_widget)
        else:
            self.subtask_widget.set_trace(self._subtask_trace)
        return self.subtask_widget

    def reveal_subtask(self):
        widget = self._ensure_subtask_widget()
        if widget is not None:
            widget.set_expanded(True)
        return widget

    def _sync_detail_aliases(self) -> None:
        if self.detail_panel is None:
            return
        self.args_view = self.detail_panel.args_view
        self.result_label = self.detail_panel.result_label
        self.result_view = self.detail_panel.result_view

    def _toggle(self):
        self.set_expanded(not self.is_expanded)

    def set_expanded(self, expanded: bool) -> None:
        self.is_expanded = bool(expanded)
        details_widget = self._ensure_details_widget() if self.is_expanded else self.details_widget
        if details_widget is not None:
            details_widget.setVisible(self.is_expanded)
        if self.toggle_btn is not None:
            self.toggle_btn.set_expanded(self.is_expanded)
        if self.is_expanded:
            self._refit_details()

    def _tool_name(self) -> str:
        return tool_call_name(self.tool_call) or "unknown_tool"

    def _tool_kind(self, name: str | None = None) -> str:
        return tool_call_kind(name or self._tool_name())

    def _display_name(self, name: str | None = None) -> str:
        return _tool_call_display_name(name or self._tool_name())

    def _kind_label(self, kind: str | None = None) -> str:
        return _tool_call_kind_label(kind or self._tool_kind())

    def _header_icon(self, kind: str | None = None, name: str | None = None):
        kind = kind or self._tool_kind()
        name = str(name or self._tool_name() or "").lower()
        if kind == 'subagent':
            return Icons.get_muted(Icons.BOT, scale_factor=1.0)
        if kind == 'capability':
            return Icons.get_muted(Icons.WAND, scale_factor=1.0)
        if name.startswith(("shell__", "python__")) or "terminal" in name or "command" in name:
            return Icons.get_muted(Icons.TERMINAL, scale_factor=1.0)
        icon = {
            'file__read': Icons.FILE_LINES, 'file__list': Icons.FOLDER,
            'file__search': Icons.SEARCH, 'file__ocr': Icons.OCR,
            'file__write': Icons.EDIT, 'file__edit': Icons.EDIT,
            'file__patch': Icons.EDIT, 'file__delete': Icons.TRASH,
            'file__deliver': Icons.FILE, 'web__search': Icons.SEARCH,
            'web__fetch': Icons.GLOBE, 'state__artifact': Icons.DOCUMENT,
            'state__memory': Icons.MEMORY, 'state__wiki': Icons.BOOK,
            'state__todo': Icons.CHECK, 'user__ask': Icons.MESSAGE,
            'agent__complete': Icons.CIRCLE_CHECK,
            'archive__read': Icons.FILE_LINES, 'archive__list': Icons.FOLDER,
        }.get(name)
        if icon:
            return Icons.get_muted(icon, scale_factor=1.0)
        return Icons.get_muted(Icons.WRENCH, scale_factor=1.0)

    def _target(self, *, full: bool = False) -> str:
        name = self._tool_name()
        args = _coerce_tool_arguments(self.tool_call)
        if name.startswith('file__') and name != 'file__search':
            path = str(args.get('path') or '')
            target = path if full or not path else _file_name_for_display(path)
            if name == 'file__read' and (args.get('start_line') or args.get('end_line')):
                start, end = args.get('start_line') or 1, args.get('end_line')
                target += f' :{start}–{end}' if end else f' :{start}+'
            return target.strip()
        key = {
            'file__search': 'query', 'web__search': 'query', 'web__fetch': 'url',
            'shell__run': 'command', 'shell__read': 'process_id',
            'shell__write': 'process_id', 'shell__kill': 'process_id',
            'state__artifact': 'name', 'agent__run': 'agent_id',
        }.get(name)
        return str(args.get(key) or '') if key else ''

    def _action_title(self, name: str) -> str:
        target = _plain_summary(self._target(), 120)
        return ' · '.join(part for part in (self._display_name(name), target) if part)

    def _header_tooltip(self, detail: str = '') -> str:
        parts = [QCoreApplication.translate('ToolCallView', '{value}调用：{name}').format(
            value=self._kind_label(), name=self._tool_name()), self._target(full=True), detail]
        return '\n'.join(part for part in parts if part)

    def _running_title(self, name: str) -> str:
        if name == 'user__ask':
            return QCoreApplication.translate('ToolCallView', '等待你的选择')
        return QCoreApplication.translate('ToolCallView', '运行中 · {value}').format(value=self._action_title(name))

    def set_running_detail(self, detail: str) -> None:
        if self.result_payload is not None or self.toggle_btn is None:
            return
        clean = str(detail or "").strip()
        if not clean:
            self.toggle_btn.setText(self._running_title(self._tool_name()))
            return
        self.toggle_btn.setText(f"{self._action_title(self._tool_name())} · {_plain_summary(clean, 80)}")
        self.toggle_btn.setToolTip(self._header_tooltip(clean))

    def _completed_title_with_summary(self, name: str, summary: str = "") -> str:
        title = self._action_title(name)
        clean = _plain_summary(summary, 48) if not self._target() else ''
        return f'{title} · {clean}' if clean else title

    def _failed_title_with_summary(self, name: str, summary: str = "") -> str:
        title = QCoreApplication.translate('ToolCallView', '运行失败 · {value}').format(value=self._action_title(name))
        clean = _plain_summary(summary, 48)
        if clean:
            return f"{title} · {clean}"
        return title

    def _result_summary(self, result: Any, *, limit: int = 48) -> str:
        if isinstance(result, dict):
            summary = str(result.get('summary') or '').strip()
            if summary:
                return _plain_summary(summary, limit)
            result = result.get('content') or result.get('final_message') or ''

        text = str(result or '').strip()
        if not text:
            return ''
        first_line = text.splitlines()[0].strip()
        return _plain_summary(first_line, limit)

    def _full_result_summary(self, result: Any) -> str:
        if isinstance(result, dict):
            summary = str(result.get('summary') or '').strip()
            if summary:
                return _plain_summary(summary, 600)
            result = result.get('content') or result.get('final_message') or ''
        text = str(result or '').strip()
        return _plain_summary(text, 600)

    def _result_meta_hint(self) -> str:
        return ''

    def _add_file_change_summary(self, parent_layout: QVBoxLayout) -> None:
        name = self._tool_name()
        if name not in EDIT_TOOL_NAMES:
            return
        args = _coerce_tool_arguments(self.tool_call)
        structured_change = None
        result_metadata = self.result_payload.get("metadata") if isinstance(self.result_payload, dict) else {}
        raw_change = result_metadata.get("file_change") if isinstance(result_metadata, dict) else None
        if isinstance(raw_change, dict):
            try:
                structured_change = FileChange.from_dict(raw_change)
            except (TypeError, ValueError):
                structured_change = None
        path = str((structured_change.path if structured_change is not None else "") or args.get("path") or "").strip()
        if not path:
            return

        action, action_label = _file_change_action(name)
        if structured_change is not None:
            action = structured_change.action
            action_label = {
                "write": QCoreApplication.translate('ToolCallView', '已写入'),
                "edit": QCoreApplication.translate('ToolCallView', '已编辑'),
                "patch": QCoreApplication.translate('ToolCallView', '已应用补丁'),
                "delete": QCoreApplication.translate('ToolCallView', '已删除'),
            }.get(action, action_label)
        row_status = "completed"
        if structured_change is not None and not structured_change.is_successful:
            row_status = "failed"
        row = WorkflowCapsuleRow(kind="file", status=row_status, payload=path, file_path=path, work_dir=self.work_dir)
        row.clicked.connect(lambda _payload, capsule=row: capsule.open_file())
        icon_text = "±"
        if action == "write":
            icon_text = "+"
        elif action == "delete":
            icon_text = "x"
            if structured_change is None:
                row.set_kind_status(status="failed")
        elif action == "patch":
            icon_text = "~"
        row.set_content(icon=icon_text, title=f"{action_label} {_file_name_for_display(path)}")
        row.icon_label.setProperty("action", action)
        row.icon_label.style().unpolish(row.icon_label)
        row.icon_label.style().polish(row.icon_label)

        self.file_change_widget = row
        self.file_change_title = row.title_label
        self.file_change_meta = row.meta_label
        self.file_change_icon = row.icon_label
        self._refresh_file_change_summary()
        parent_layout.addWidget(row)

    def _refresh_file_change_summary(self) -> None:
        if self.file_change_widget is None:
            return
        args = _coerce_tool_arguments(self.tool_call)
        path = str(args.get("path") or "").strip()
        meta_text = _file_change_meta(self._tool_name(), args, self.result_payload)
        result_text = ""
        if isinstance(self.result_payload, dict):
            result_text = str(self.result_payload.get("summary") or self.result_payload.get("content") or "")
            result_metadata = self.result_payload.get("metadata")
            raw_change = result_metadata.get("file_change") if isinstance(result_metadata, dict) else None
            if isinstance(raw_change, dict):
                try:
                    change = FileChange.from_dict(raw_change)
                except (TypeError, ValueError):
                    change = None
                if change is not None and change.summary:
                    result_text = change.summary
        if self.file_change_meta is not None:
            self.file_change_meta.setText(meta_text)
            self.file_change_meta.setVisible(bool(meta_text))
        tooltip_parts = [path]
        if isinstance(self.file_change_widget, WorkflowCapsuleRow):
            self.file_change_widget.set_work_dir(self.work_dir)
            self.file_change_widget.set_payload(path)
            try:
                tooltip_parts.append(QCoreApplication.translate('ToolCallView', '打开: {value}').format(value=self.file_change_widget.resolved_path()))
            except Exception as exc:
                logger.debug("Failed to resolve changed file path for tooltip: %s", exc)
        if meta_text:
            tooltip_parts.append(meta_text)
        if result_text:
            tooltip_parts.append(_plain_summary(result_text, 260))
        if isinstance(self.result_payload, dict):
            result_metadata = self.result_payload.get("metadata")
            raw_change = result_metadata.get("file_change") if isinstance(result_metadata, dict) else None
            if isinstance(raw_change, dict):
                try:
                    change = FileChange.from_dict(raw_change)
                except (TypeError, ValueError):
                    change = None
                if change is not None and (change.before_digest or change.after_digest):
                    tooltip_parts.append(
                        QCoreApplication.translate('ToolCallView', '前: {value}\n后: {value_}').format(value=change.before_digest or '-', value_=change.after_digest or '-')
                    )
        self.file_change_widget.setToolTip("\n".join(part for part in tooltip_parts if part))

    def _lookup_artifact(self, name: str, fallback: object) -> object:
        if self.artifact_lookup is not None:
            try:
                resolved = self.artifact_lookup(str(name or ""))
                if resolved is not None:
                    return resolved
            except Exception as exc:
                logger.debug("Failed to resolve artifact %s for tool capsule: %s", name, exc)
        return fallback

    def _artifact_candidates(self) -> list[tuple[str, str, object]]:
        result = self.result_payload or {}
        if self._tool_name() == "state__artifact" and (
            result.get("is_error") or (result.get("metadata") or {}).get("is_error")
        ):
            return []
        candidates: list[tuple[str, str, object]] = []
        seen: set[str] = set()
        args = _coerce_tool_arguments(self.tool_call)

        if self._tool_name() == "state__artifact":
            action = str(args.get("action") or "").strip().lower()
            name = str(args.get("name") or "").strip()
            if name and action in {"create", "upsert", "update", "append", "delete"}:
                labels = {
                    "create": QCoreApplication.translate('ToolCallView', '已创建'),
                    "upsert": QCoreApplication.translate('ToolCallView', '已保存'),
                    "update": QCoreApplication.translate('ToolCallView', '已更新'),
                    "append": QCoreApplication.translate('ToolCallView', '已追加'),
                    "delete": QCoreApplication.translate('ToolCallView', '已删除'),
                }
                fallback = {
                    "name": name,
                    "kind": str(args.get("kind") or "artifact"),
                    "status": str(args.get("status") or ("archived" if action == "delete" else "draft")),
                    "content": str(args.get("content") or ""),
                    "content_chars": len(str(args.get("content") or "")),
                }
                candidates.append((name, f"{labels[action]} {name}", self._lookup_artifact(name, fallback)))
                seen.add(name)

        metadata_sources: list[dict[str, Any]] = []
        if isinstance(self.result_payload, dict):
            metadata = self.result_payload.get("metadata")
            if isinstance(metadata, dict):
                metadata_sources.append(metadata)
            run = self.result_payload.get("run")
            if isinstance(run, dict) and isinstance(run.get("metadata"), dict):
                metadata_sources.append(run["metadata"])

        for metadata in metadata_sources:
            for item in metadata.get("imported_artifacts") or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if not name or name in seen:
                    continue
                candidates.append((name, QCoreApplication.translate('ToolCallView', '已导入 {name}').format(name=name), self._lookup_artifact(name, item)))
                seen.add(name)
        return candidates

    def _refresh_artifact_capsules(self) -> None:
        if self.capsule_layout is None:
            return
        for row in self.artifact_widgets:
            self.capsule_layout.removeWidget(row)
            row.deleteLater()
        self.artifact_widgets = []

        for name, title, artifact in self._artifact_candidates():
            row = create_artifact_capsule(name, artifact, title=title, work_dir=self.work_dir)
            row.clicked.connect(lambda _payload, capsule=row: capsule.open_file())
            self.capsule_layout.addWidget(row)
            self.artifact_widgets.append(row)

    def _ensure_details_widget(self) -> QWidget:
        if self.details_widget is not None:
            return self.details_widget

        details_widget = QWidget()
        details_widget.setObjectName("tool_details")
        details_widget.setVisible(False)
        details_layout = QVBoxLayout(details_widget)
        # Reserve the structural accent line so raw argument/result surfaces
        # cannot paint over the continuous line owned by tool_details.
        details_layout.setContentsMargins(2, 3, 0, 0)
        details_layout.setSpacing(4)

        self.details_widget = details_widget
        self.details_layout = details_layout
        self.detail_panel = ToolDetailPanel(self.tool_call, self.result_payload)
        details_layout.addWidget(self.detail_panel)
        self._sync_detail_aliases()
        insert_at = self.layout().count()
        if self.capsule_container is not None:
            capsule_index = self.layout().indexOf(self.capsule_container)
            if capsule_index >= 0:
                insert_at = capsule_index
        self.layout().insertWidget(insert_at, details_widget)
        return details_widget

    def _apply_result_details(self, result_payload: dict[str, Any]) -> None:
        if self.detail_panel is not None:
            self.detail_panel.set_result(result_payload)
            self._sync_detail_aliases()

        meta_hint = self._result_meta_hint()
        if self.meta_label is not None:
            self.meta_label.setVisible(bool(meta_hint))
            self.meta_label.setText(meta_hint)

    def _refit_details(self) -> None:
        if self.detail_panel is not None:
            self.detail_panel.refit_height()

    def _apply_result_header(self, result: Any) -> None:
        name = self._tool_name()
        summary = self._result_summary(result)
        metadata = dict(result.get('metadata') or {}) if isinstance(result, dict) and isinstance(result.get('metadata'), dict) else {}
        is_error = bool(
            isinstance(result, dict)
            and (result.get('is_error') or result.get('error') or metadata.get('is_error'))
        )
        if self.toggle_btn is not None:
            self.toggle_btn.setProperty('status', 'failed' if is_error else 'completed')
            self.toggle_btn.style().unpolish(self.toggle_btn)
            self.toggle_btn.style().polish(self.toggle_btn)
            self.toggle_btn.setText(
                self._failed_title_with_summary(name, summary)
                if is_error
                else self._completed_title_with_summary(name, summary)
            )
            full_summary = self._full_result_summary(result)
            tooltip_parts = [self._header_tooltip()]
            if is_error:
                tooltip_parts.append(QCoreApplication.translate('ToolCallView', '状态：失败'))
            if full_summary:
                tooltip_parts.append(full_summary)
            self.toggle_btn.setToolTip("\n".join(tooltip_parts))
        if self.summary_label is not None:
            self.summary_label.setVisible(False)

    def _placeholder_subtask_trace(self, result_payload: dict | None = None) -> dict[str, Any]:
        payload = result_payload or {}
        kind = self._tool_kind()
        metadata = dict(payload.get('metadata') or {}) if isinstance(payload.get('metadata'), dict) else {}
        args = _coerce_tool_arguments(self.tool_call)
        final_message = str(payload.get('content') or payload.get('summary') or '').strip()
        status = str(metadata.get('subtask_status') or metadata.get('status') or '').strip()
        if not status:
            status = 'completed' if final_message else 'running'
        title = str(args.get('agent_id') or args.get('name') or self._display_name()).strip() or self._display_name()
        return {
            'id': str(self.tool_id or ''),
            'kind': kind,
            'name': self._tool_name(),
            'title': title,
            'status': status,
            'messages': [],
            'final_message': final_message,
            'goal': final_message,
            'duration_ms': 0,
            'metadata': metadata,
        }

    def set_result(self, result: Any):
        result_payload = normalize_tool_result(result)
        self.result_payload = result_payload
        if result_payload.get('type') == 'subtask_run':
            run = result_payload.get('run')
            self.set_subtask(run if isinstance(run, dict) else self._placeholder_subtask_trace(result_payload))
            self._apply_result_header(result_payload)
            return
        if self._tool_kind() in {'subagent', 'capability'}:
            self.set_subtask(self._placeholder_subtask_trace(result_payload))
            return
        self._apply_result_header(result_payload)
        if self.summary_label is not None:
            self.summary_label.setVisible(False)
        self._refresh_file_change_summary()
        self._refresh_artifact_capsules()
        if self.details_widget is not None or self.is_expanded:
            self._ensure_details_widget()
            self._apply_result_details(result_payload)

    def update_content(self):
        """Refresh content from the normalized invocation state."""
        if self.result_payload is not None:
            self.set_result(self.result_payload)


class ToolCallsSection(QWidget):
    """Container for normalized tool invocation views."""

    def __init__(
        self,
        tool_calls: List[dict] | List[ToolInvocationView],
        parent=None,
        *,
        work_dir: str = "",
        artifact_lookup: Callable[[str], object | None] | None = None,
        embedded_message_factory: Callable[..., object] | None = None,
    ):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.work_dir = str(work_dir or "")
        self.artifact_lookup = artifact_lookup
        self._embedded_message_factory = embedded_message_factory
        self.invocations: list[ToolInvocationView] = [item for item in tool_calls if isinstance(item, ToolInvocationView)]
        self.tool_calls = [item.tool_call if isinstance(item, ToolInvocationView) else item for item in tool_calls]
        self.items = {}
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(2)

        sources = self.invocations if self.invocations else self.tool_calls
        for source in sources:
            tool_call = source.tool_call if isinstance(source, ToolInvocationView) else source
            if not isinstance(tool_call, dict):
                continue
            item = ToolCallItem(
                source,
                work_dir=self.work_dir,
                artifact_lookup=self.artifact_lookup,
                embedded_message_factory=self._embedded_message_factory,
            )
            self.items[tool_call.get('id')] = item
            layout.addWidget(item)

    def update_subtask(self, tool_id: str, trace: dict):
        if tool_id in self.items:
            self.items[tool_id].set_subtask(trace)

    def update_status(self, tool_id: str, detail: str) -> bool:
        item = self.items.get(str(tool_id or ""))
        if item is None:
            return False
        item.set_running_detail(detail)
        return True

    def refresh_all(self):
        """Refresh all items from their underlying data"""
        for item in self.items.values():
            item.update_content()

    def expanded_tool_ids(self) -> set[str]:
        return {
            str(tool_id)
            for tool_id, item in self.items.items()
            if tool_id and bool(item.is_expanded)
        }

    def restore_expanded_tool_ids(self, tool_ids: Iterable[str]) -> None:
        expanded = {str(tool_id) for tool_id in tool_ids or ()}
        for tool_id, item in self.items.items():
            item.set_expanded(str(tool_id) in expanded)

    def set_work_dir(self, work_dir: str) -> None:
        self.work_dir = str(work_dir or "")
        for item in self.items.values():
            item.set_work_dir(self.work_dir)


class SubtaskRunWidget(QWidget):
    """Collapsible child-agent run rendered from a normalized RunTree node."""

    def __init__(
        self,
        trace: dict[str, Any],
        parent=None,
        *,
        work_dir: str = "",
        embedded_message_factory: Callable[..., object] | None = None,
    ):
        super().__init__(parent)
        self.trace = self._normalize_trace(trace)
        self.work_dir = str(work_dir or "")
        self._embedded_message_factory = embedded_message_factory
        self.is_expanded = False
        self.toggle_btn = None
        self.summary_row = None
        self.summary_icon = None
        self.summary_title = None
        self.summary_meta = None
        self.summary_label = None
        self.content_widget = None
        self.show_more_btn = None
        self._show_all = False
        self._content_built = False
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 1, 0, 1)
        layout.setSpacing(2)

        self.summary_row = WorkflowCapsuleRow(kind="subagent", status="completed")
        self.summary_row.setProperty("expandable", True)
        self.summary_row.clicked.connect(lambda _payload=None: self._toggle())
        self.summary_icon = self.summary_row.icon_label
        self.summary_title = self.summary_row.title_label
        self.summary_meta = self.summary_row.meta_label
        layout.addWidget(self.summary_row)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        self.summary_label.setProperty("muted", True)
        self.summary_label.setVisible(False)
        layout.addWidget(self.summary_label)

        self.content_widget = QWidget()
        self.content_widget.setObjectName("subagent_trace_content")
        self.content_widget.setVisible(False)
        content_layout = QVBoxLayout(self.content_widget)
        content_layout.setContentsMargins(10, 6, 4, 6)
        content_layout.setSpacing(4)
        layout.addWidget(self.content_widget)
        self._refresh_header()

    def _show_all_messages(self):
        self._show_all = True
        self._rebuild_content()

    def set_trace(self, trace: dict[str, Any]) -> None:
        self.trace = self._normalize_trace(trace)
        self._refresh_header()
        if self.is_expanded:
            self._rebuild_content()

    def set_work_dir(self, work_dir: str) -> None:
        self.work_dir = str(work_dir or "")
        if self.content_widget is None:
            return
        content_layout = self.content_widget.layout()
        for index in range(content_layout.count()):
            widget = content_layout.itemAt(index).widget()
            if hasattr(widget, "set_work_dir"):
                widget.set_work_dir(self.work_dir)

    def _toggle(self):
        self.set_expanded(not self.is_expanded)

    def set_expanded(self, expanded: bool):
        self.is_expanded = bool(expanded)
        if self.content_widget is not None:
            self.content_widget.setVisible(self.is_expanded)
        if self.summary_row is not None:
            self.summary_row.setProperty("expanded", self.is_expanded)
            self.summary_row.style().unpolish(self.summary_row)
            self.summary_row.style().polish(self.summary_row)
        if self.is_expanded:
            self._rebuild_content()
        else:
            self._clear_content()

    @classmethod
    def _normalize_trace(cls, trace: dict[str, Any]) -> dict[str, Any]:
        run = normalize_subtask_run(dict(trace or {}))
        cls._finalize_unresolved_tool_calls(run)
        return run

    @staticmethod
    def _finalize_unresolved_tool_calls(trace: dict[str, Any]) -> None:
        status = str(trace.get('status') or '').strip().lower()
        if status in {'', 'running'}:
            return
        failed = status in {'failed', 'cancelled', 'failed_partial'}
        content = (
            "Subtask ended before this tool result was captured."
            if failed
            else "Tool completed; result was not captured in the subtask trace."
        )
        for message in trace.get('messages') or []:
            if not isinstance(message, dict) or message.get('role') != 'assistant':
                continue
            for tool_call in message.get('tool_calls') or []:
                if not isinstance(tool_call, dict):
                    continue
                result = tool_call.get('result')
                if isinstance(result, dict) and (result.get('content') or result.get('summary')):
                    continue
                tool_call['result'] = normalize_tool_result(
                    {
                        'type': 'tool_result',
                        'content': content,
                        'summary': content[:220],
                        'metadata': {
                            'status': 'unknown',
                            'subtask_status': status,
                            'is_error': failed,
                        },
                    }
                )

    def _trace_metadata(self) -> dict[str, Any]:
        metadata = self.trace.get('metadata') if isinstance(self.trace.get('metadata'), dict) else {}
        return dict(metadata or {})

    def _metadata_value(self, *keys: str) -> Any:
        metadata = self._trace_metadata()
        for key in keys:
            value = self.trace.get(key)
            if value not in (None, "", []):
                return value
            value = metadata.get(key)
            if value not in (None, "", []):
                return value
        return ""

    def _child_artifact(self, name: str) -> object | None:
        """Prefer the child receipt over a possibly unrelated parent artifact."""
        for item in self._trace_metadata().get("produced_refs") or []:
            if not isinstance(item, dict) or item.get("type") != "artifact":
                continue
            identifier = str(item.get("name") or item.get("id") or "").strip()
            if identifier.lower() != str(name).strip().lower():
                continue
            return ContentRef(
                id=identifier, name=f"{identifier}.md", kind="artifact", ref=f"artifact:{identifier}",
                mime="text/markdown", size=int(item.get("chars") or 0), digest=str(item.get("digest") or ""),
                locator=str(item.get("path") or ""), source="artifact", status=str(item.get("status") or "draft"),
                workspace=str(item.get("workspace", self.work_dir) or ""),
                conversation_id=str(item.get("source_session_id") or self._metadata_value("child_session_id") or ""),
            )
        return None

    def _trace_tool_count(self) -> int:
        value = self._metadata_value("tool_count")
        try:
            count = int(value or 0)
        except Exception:
            count = 0
        if count > 0:
            return count
        total = 0
        for message in self.trace.get('messages') or []:
            if isinstance(message, dict) and isinstance(message.get('tool_calls'), list):
                total += len(message.get('tool_calls') or [])
        return total

    @staticmethod
    def _summary_status(status: str, partial: Any = None) -> tuple[str, str, str]:
        normalized = str(status or "").strip().lower()
        partial_text = str(partial or "").strip().lower()
        is_partial = normalized == "failed_partial" or partial is True or partial_text in {"1", "true", "yes"}
        if normalized == "running":
            return "~", QCoreApplication.translate('ToolCallView', '运行中'), "running"
        if is_partial:
            return "!", QCoreApplication.translate('ToolCallView', '部分完成'), "partial"
        if normalized in {"failed", "cancelled"}:
            return "!", QCoreApplication.translate('ToolCallView', '失败'), "failed"
        return "+", QCoreApplication.translate('ToolCallView', '已完成'), "completed"

    def _refresh_header(self):
        status = str(self.trace.get('status') or 'completed').strip().lower()
        title = str(self.trace.get('title') or self.trace.get('name') or 'Subagent').strip()
        kind = str(self.trace.get('kind') or 'subagent').strip().lower()
        messages = [m for m in self.trace.get('messages') or [] if isinstance(m, dict)]
        duration_ms = int(self.trace.get('duration_ms') or 0)
        duration = f"{duration_ms / 1000:.1f}s" if duration_ms > 0 else ""
        child_session_id = str(self._metadata_value("child_session_id", "session_id") or "").strip()
        effective_turns = self._metadata_value("effective_max_turns")
        tool_count = self._trace_tool_count()
        partial = self._metadata_value("partial")
        icon_text, action_label, status_key = self._summary_status(status, partial)

        if self.summary_icon is not None:
            self.summary_row.set_kind_status(kind=kind if kind == "capability" else "subagent", status=status_key)

        meta_parts: list[str] = []
        if child_session_id:
            meta_parts.append(QCoreApplication.translate('ToolCallView', '会话 {value}').format(value=child_session_id[:8]))
        if effective_turns not in (None, "", []):
            meta_parts.append(QCoreApplication.translate('ToolCallView', '轮次 {effective_turns}').format(effective_turns=effective_turns))
        if tool_count > 0:
            meta_parts.append(QCoreApplication.translate('ToolCallView', '工具 {tool_count}').format(tool_count=tool_count))
        if messages:
            meta_parts.append(QCoreApplication.translate('ToolCallView', '消息 {value}').format(value=len(messages)))
        if duration:
            meta_parts.append(duration)
        if self.summary_row is not None:
            self.summary_row.set_content(
                icon=icon_text,
                title=f"{action_label} {title}",
                meta=" · ".join(meta_parts),
            )

        if self.summary_row is not None:
            kind_label = QCoreApplication.translate('ToolCallView', '能力') if kind == "capability" else QCoreApplication.translate('ToolCallView', '子 Agent')
            full_summary = _plain_summary(
                self.trace.get('final_message') or self.trace.get('error') or self.trace.get('goal'),
                limit=600,
            )
            tooltip_parts = [f"{kind_label}: {title}", f"status: {status or 'completed'}"]
            if child_session_id:
                tooltip_parts.append(f"session: {child_session_id}")
            if effective_turns not in (None, "", []):
                tooltip_parts.append(f"effective_max_turns: {effective_turns}")
            if tool_count > 0:
                tooltip_parts.append(f"tool_count: {tool_count}")
            if not messages:
                tooltip_parts.append(QCoreApplication.translate('ToolCallView', '详细消息未保存在此归档中'))
            if full_summary:
                tooltip_parts.append(full_summary)
            self.summary_row.setToolTip("\n".join(tooltip_parts))

        if self.summary_label is not None:
            self.summary_label.setVisible(False)
            self.summary_label.setText("")

    def _clear_content(self):
        if self.content_widget is None:
            return
        content_layout = self.content_widget.layout()
        while content_layout.count():
            item = content_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._content_built = False

    def _rebuild_content(self):
        if self.content_widget is None:
            return
        self._clear_content()
        content_layout = self.content_widget.layout()
        messages = [m for m in self.trace.get('messages') or [] if isinstance(m, dict)]

        detail_lines: list[str] = []
        child_session_id = str(self._metadata_value("child_session_id", "session_id") or "").strip()
        parent_session_id = str(self._metadata_value("parent_session_id") or "").strip()
        effective_turns = self._metadata_value("effective_max_turns")
        if child_session_id:
            detail_lines.append(QCoreApplication.translate('ToolCallView', '子会话：{child_session_id}').format(child_session_id=child_session_id))
        if parent_session_id:
            detail_lines.append(QCoreApplication.translate('ToolCallView', '父会话：{parent_session_id}').format(parent_session_id=parent_session_id))
        budget_parts: list[str] = []
        if effective_turns not in (None, "", []):
            budget_parts.append(QCoreApplication.translate('ToolCallView', '生效轮次 {effective_turns}').format(effective_turns=effective_turns))
        if budget_parts:
            detail_lines.append(QCoreApplication.translate('ToolCallView', '预算：') + "，".join(budget_parts))
        tool_count = self._trace_tool_count()
        if tool_count > 0:
            detail_lines.append(QCoreApplication.translate('ToolCallView', '工具调用：{tool_count}').format(tool_count=tool_count))
        if detail_lines:
            meta_label = QLabel("\n".join(detail_lines))
            meta_label.setWordWrap(True)
            meta_label.setProperty("muted", True)
            content_layout.addWidget(meta_label)

        goal = str(self.trace.get('goal') or '').strip()
        if goal:
            goal_label = QLabel(QCoreApplication.translate('ToolCallView', '目标：{value}').format(value=_plain_summary(goal, 240)))
            goal_label.setWordWrap(True)
            goal_label.setProperty("muted", True)
            content_layout.addWidget(goal_label)

        if not messages:
            empty_label = QLabel(QCoreApplication.translate('ToolCallView', '此记录只保存了子 Agent 摘要，没有保存可展开的内部消息。'))
            empty_label.setWordWrap(True)
            empty_label.setProperty("muted", True)
            content_layout.addWidget(empty_label)

        view_model = build_message_tree_view_model(messages)
        render_messages = []
        seen_message_ids: set[str] = set()
        for view in view_model.messages:
            message_id = str(getattr(view.message, 'id', '') or '')
            if message_id and message_id in seen_message_ids:
                continue
            if message_id:
                seen_message_ids.add(message_id)
            render_messages.append(view.message)
        limit = len(render_messages) if self._show_all else 80
        factory = self._embedded_message_factory

        if factory is not None:
            for child_message in render_messages[:limit]:
                try:
                    content_layout.addWidget(
                        factory(
                            child_message,
                            work_dir=self.work_dir,
                            artifact_lookup=self._child_artifact,
                        )
                    )
                except Exception as exc:
                    logger.debug("Failed to render subtask message: %s", exc)
            if len(render_messages) > limit:
                self.show_more_btn = QPushButton(QCoreApplication.translate('ToolCallView', '显示全部 {value} 条子任务消息（还有 {value_} 条）').format(value=len(render_messages), value_=len(render_messages) - limit))
                self.show_more_btn.setProperty("secondary", True)
                self.show_more_btn.clicked.connect(self._show_all_messages)
                content_layout.addWidget(self.show_more_btn)

        error = str(self.trace.get('error') or '').strip()
        if error:
            error_label = QLabel(QCoreApplication.translate('ToolCallView', '错误:'))
            error_label.setObjectName("message_error_label")
            content_layout.addWidget(error_label)
            error_view = MarkdownView(error)
            error_view.set_height_adjustment(minimum_height=22, padding=2)
            content_layout.addWidget(error_view)
        self._content_built = True


SubagentTraceItem = SubtaskRunWidget
