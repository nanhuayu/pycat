"""
Message widget for displaying individual messages - Responsive layout
"""

import json
import logging
import math
import re
from typing import List, Optional, Any

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFrame, QSizePolicy, QToolButton, QTextBrowser, QAbstractScrollArea,
    QButtonGroup, QCheckBox, QLineEdit, QRadioButton
)
from PyQt6.QtCore import pyqtSignal, Qt, QTimer, QSize
from PyQt6.QtGui import QTextOption, QGuiApplication, QCursor

try:
    import markdown
except ImportError:
    markdown = None

from models.conversation import Message
from models.conversation import normalize_tool_result
from ui.view_models.message_tree import ToolInvocationView, build_message_tree_view_model, view_model_for_message
from ui.dialogs.image_viewer import ImageViewerDialog
from ui.utils.image_loader import load_pixmap
from ui.utils.icon_manager import Icons


logger = logging.getLogger(__name__)


MESSAGE_HEADER_HEIGHT = 22
MESSAGE_BADGE_HEIGHT = 20
MESSAGE_ACTION_SIZE = 22


def _tool_call_name(tool_call: dict | None) -> str:
    func = (tool_call or {}).get('function', {})
    return str(func.get('name', 'unknown_tool') or 'unknown_tool')


def _tool_call_kind(name: str) -> str:
    if str(name or '').startswith('subagent__'):
        return 'subagent'
    if str(name or '').startswith('capability__'):
        return 'capability'
    return 'tool'


def _tool_call_display_name(name: str) -> str:
    text = str(name or 'unknown_tool')
    if text.startswith('subagent__'):
        return text.removeprefix('subagent__')
    if text.startswith('capability__'):
        return text.removeprefix('capability__')
    return text


def _tool_call_kind_label(kind: str) -> str:
    return {
        'subagent': '子 Agent',
        'capability': '能力',
        'tool': '工具',
    }.get(kind, '工具')


def _plain_summary(text: Any, limit: int = 160) -> str:
    value = str(text or '').strip().replace('\r\n', '\n').replace('\r', '\n')
    value = re.sub(r"\s+", " ", value)
    if len(value) > limit:
        return value[: max(0, limit - 1)] + '…'
    return value


def _subtask_status_label(status: str) -> str:
    return {
        'running': '运行中',
        'completed': '已完成',
        'cancelled': '已取消',
        'failed': '失败',
    }.get(str(status or '').lower(), str(status or '未知'))


def _subtask_status_icon(status: str) -> str:
    return {
        'running': '◐',
        'completed': '✓',
        'cancelled': '○',
        'failed': '✗',
    }.get(str(status or '').lower(), '•')


MARKDOWN_CSS = """
<style>
    body { margin: 0; padding: 0; }
    p { margin-bottom: 2px; margin-top: 0; }
    ul, ol { margin-top: 2px; margin-bottom: 2px; padding-left: 18px; }
    li { margin-top: 0; margin-bottom: 2px; }

    /* Headings */
    h1, h2, h3, h4, h5, h6 {
        margin-top: 10px; margin-bottom: 5px;
        font-weight: 600;
    }

    /* Code blocks */
    pre {
        background-color: rgba(128, 128, 128, 0.15);
        padding: 8px 10px;
        border-radius: 6px;
        margin: 6px 0;
        max-width: 100%;
        white-space: pre-wrap;
        word-wrap: break-word;
        overflow-wrap: anywhere;
    }
    code {
        background-color: rgba(128, 128, 128, 0.15);
        padding: 2px 4px;
        border-radius: 4px;
        font-family: "Consolas", "Monaco", monospace;
        white-space: pre-wrap;
        word-wrap: break-word;
        overflow-wrap: anywhere;
        word-break: break-word;
    }
    pre code {
        background-color: transparent;
        padding: 0;
        border-radius: 0;
        white-space: pre-wrap;
    }

    /* Tables */
    table {
        border-collapse: collapse;
        width: 100%;
        margin: 6px 0;
        border: 1px solid rgba(128, 128, 128, 0.3);
    }
    th {
        background-color: rgba(128, 128, 128, 0.1);
        font-weight: 700;
        padding: 6px;
        border: 1px solid rgba(128, 128, 128, 0.3);
        text-align: left;
    }
    td {
        padding: 6px;
        border: 1px solid rgba(128, 128, 128, 0.3);
    }

    /* Blockquotes */
    blockquote {
        border-left: 4px solid #5b7cfa;
        background-color: rgba(91, 124, 250, 0.12);
        padding: 6px 8px;
        margin: 6px 0;
        color: inherit;
        border-radius: 0 6px 6px 0;
    }
    blockquote p { margin-bottom: 4px; }
    ::selection {
        background-color: rgba(91, 124, 250, 0.45);
        color: #ffffff;
    }

    /* Links */
    a { color: #2962ff; text-decoration: none; }
</style>
"""


_FENCE_LINE_RE = re.compile(r"^\s{0,3}(```|~~~)")
_LIST_ITEM_RE = re.compile(r"^\s{0,3}(?:[-+*]\s+|\d+[.)]\s+)")


def _normalize_markdown_for_view(text: str) -> str:
    """Normalize LLM-flavored markdown so QTextDocument renders lists reliably.

    Python-Markdown treats a list marker directly after a paragraph-like line as
    plain text unless there is a blank line. LLMs often emit compact sections like
    ``**标题**\n- item``. Insert the missing structural blank lines outside code
    fences while keeping already-valid markdown unchanged.
    """
    source = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = source.split("\n")
    out: list[str] = []
    in_fence = False
    previous_was_list = False

    for line in lines:
        stripped = line.strip()
        fence = bool(_FENCE_LINE_RE.match(line))
        if fence:
            if out and out[-1].strip() and not in_fence:
                out.append("")
            out.append(line)
            in_fence = not in_fence
            previous_was_list = False
            continue

        if in_fence:
            out.append(line)
            continue

        is_blank = not stripped
        is_list = bool(_LIST_ITEM_RE.match(line))

        if is_list and out and out[-1].strip() and not previous_was_list:
            out.append("")
        elif previous_was_list and not is_blank and not is_list and out and out[-1].strip():
            out.append("")

        out.append(line)
        previous_was_list = bool(is_list and not is_blank)

    return "\n".join(out)


class ImageThumbnail(QLabel):
    """Clickable image thumbnail"""

    clicked = pyqtSignal()

    def __init__(self, image_data: str, parent=None):
        super().__init__(parent)
        self.setObjectName("image_thumbnail")
        self.setFixedSize(80, 80)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._load_image(image_data)

    def _load_image(self, image_data: str):
        try:
            pixmap = load_pixmap(image_data)

            if not pixmap.isNull():
                scaled = pixmap.scaled(80, 80, Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation)
                self.setPixmap(scaled)
                self.setProperty("state", "image")
                self.setText("")
            else:
                self.setProperty("state", "placeholder")
                self.setText("IMG")
                self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        except Exception:
            self.setProperty("state", "error")
            self.setText("⚠️")
            self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        finally:
            self.style().unpolish(self)
            self.style().polish(self)

    def mousePressEvent(self, event):
        self.clicked.emit()


class MarkdownView(QTextBrowser):
    """A compact, auto-height markdown-capable viewer."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setOpenExternalLinks(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self._fitting_height = False
        self._minimum_content_height = 14
        self._height_padding = 2

        doc = self.document()
        opt = doc.defaultTextOption()
        opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        doc.setDefaultTextOption(opt)
        doc.setDocumentMargin(0)

        # Monitor document size changes
        try:
            doc.documentLayout().documentSizeChanged.connect(self._on_document_size_changed)
        except Exception as exc:
            logger.debug("Failed to connect markdown document size listener: %s", exc)

        self.set_markdown(text)

    def set_markdown(self, text: str) -> None:
        if text is None:
            text = ""
        text = str(text)
        render_text = _normalize_markdown_for_view(text)

        if markdown:
            try:
                extensions = ['fenced_code', 'tables', 'sane_lists']
                html = markdown.markdown(render_text, extensions=extensions)
                self.setHtml(MARKDOWN_CSS + html)
            except Exception:
                self.document().setMarkdown(render_text)
        else:
            try:
                self.document().setMarkdown(render_text)
            except Exception:
                self.setPlainText(text)

        self.refit_height()
        QTimer.singleShot(0, self.refit_height)

    def set_height_adjustment(self, *, minimum_height: int = 14, padding: int = 2) -> None:
        """Tune auto-height for styled containers that need extra breathing room."""
        self._minimum_content_height = max(1, int(minimum_height))
        self._height_padding = max(0, int(padding))
        self.refit_height()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refit_height()

    def _on_document_size_changed(self, *_args):
        self.refit_height()

    def refit_height(self) -> None:
        if self._fitting_height:
            return
        self._fitting_height = True
        try:
            width = self.viewport().width() or self.width()
            if width <= 0:
                parent = self.parentWidget()
                width = parent.width() if parent is not None else 360
            width = max(120, int(width))
            self.document().setTextWidth(width)
            size = self.document().documentLayout().documentSize()
            height = max(self._minimum_content_height, int(math.ceil(size.height())) + self._height_padding)
            if self.height() != height:
                self.setFixedHeight(height)
            self.updateGeometry()
        except Exception as exc:
            logger.debug("Failed to refit markdown view height: %s", exc)
        finally:
            self._fitting_height = False

    def minimumSizeHint(self):
        return QSize(0, 0)

    def sizeHint(self):
        try:
            return QSize(max(120, self.width() or 360), max(14, self.height()))
        except Exception:
            return QSize(100, 16)


def _fit_text_browser_height(view: QTextBrowser, *, min_height: int = 18, max_height: int = 120) -> None:
    """Fit a QTextBrowser to its document height within a compact bound."""
    try:
        width = view.viewport().width() or view.width() or 360
        view.document().setTextWidth(max(120, int(width)))
        height = int(math.ceil(view.document().documentLayout().documentSize().height())) + 2
        view.setFixedHeight(max(min_height, min(max_height, height)))
    except Exception as exc:
        logger.debug("Failed to fit text browser height: %s", exc)


class CompactTextBrowser(QTextBrowser):
    """Read-only text browser that keeps height close to its content."""

    def __init__(self, text: str = "", parent=None, *, min_height: int = 18, max_height: int = 96):
        super().__init__(parent)
        self._min_height = min_height
        self._max_height = max_height
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.document().setDocumentMargin(2)
        self.setPlainText(text)
        self.refit_height()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refit_height()

    def refit_height(self) -> None:
        _fit_text_browser_height(self, min_height=self._min_height, max_height=self._max_height)


class ThinkingSection(QWidget):
    """Collapsible thinking section - Concise Style"""

    def __init__(self, thinking_content: str, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.thinking_content = thinking_content
        self.is_expanded = False
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 1, 0, 1)
        layout.setSpacing(0)

        self.toggle_btn = QToolButton()
        self.toggle_btn.setObjectName("thinking_toggle")
        self.toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.toggle_btn.setMaximumHeight(20)
        self.toggle_btn.setText("💭 思考过程")

        self.toggle_btn.clicked.connect(self._toggle)
        layout.addWidget(self.toggle_btn)

        self.content_widget = MarkdownView(self.thinking_content)
        self.content_widget.setObjectName("thinking_content")
        self.content_widget.document().setDocumentMargin(6)
        self.content_widget.set_height_adjustment(minimum_height=34, padding=2)
        self.content_widget.setVisible(False)
        layout.addWidget(self.content_widget)

    def _toggle(self):
        self.is_expanded = not self.is_expanded
        self.content_widget.setVisible(self.is_expanded)
        self.toggle_btn.setText("💭 思考过程" if not self.is_expanded else "💭 收起思考")
        if self.is_expanded:
            self.content_widget.refit_height()
            QTimer.singleShot(0, self.content_widget.refit_height)


class ToolCallItem(QWidget):
    """Widget for a single tool call with collapsible details - Concise Style"""

    def __init__(self, tool_call: dict | ToolInvocationView, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
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
        self.details_widget = None
        self.result_label = None
        self.result_view = None
        self.args_view = None
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 1, 0, 1)
        layout.setSpacing(2)

        func = self.tool_call.get('function', {})
        name = self._tool_name()
        kind = self._tool_kind(name)

        result_payload = self.result_payload or normalize_tool_result('')
        if kind in {'subagent', 'capability'}:
            trace = result_payload.get('run') if result_payload.get('type') == 'subtask_run' else None
            if not isinstance(trace, dict):
                trace = self._placeholder_subtask_trace(result_payload)
            self.set_subtask(trace)
            return

        # Header (Toggle button)
        self.toggle_btn = QToolButton()
        self.toggle_btn.setObjectName("tool_call_header")
        self.toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.toggle_btn.setMaximumHeight(20)
        self.toggle_btn.setProperty("kind", kind)
        self.toggle_btn.setToolTip(f"{self._kind_label(kind)}调用：{name}")

        # Initial state is "Running"
        self.toggle_btn.setText(self._running_title(name))
        self.toggle_btn.clicked.connect(self._toggle)
        layout.addWidget(self.toggle_btn)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        self.summary_label.setProperty("muted", True)
        self.summary_label.setVisible(False)
        layout.addWidget(self.summary_label)

        self.meta_label = QLabel("")
        self.meta_label.setWordWrap(True)
        self.meta_label.setProperty("muted", True)
        self.meta_label.setVisible(False)
        layout.addWidget(self.meta_label)

        # Details container (Args + Result)
        self.details_widget = QWidget()
        self.details_widget.setVisible(False)

        details_layout = QVBoxLayout(self.details_widget)
        details_layout.setContentsMargins(0, 2, 0, 0)
        details_layout.setSpacing(3)

        # Arguments (Monospace, minimal)
        args_str = func.get('arguments', '{}')
        try:
            args_obj = json.loads(args_str)
            args_display = json.dumps(args_obj, indent=2, ensure_ascii=False)
        except:
            args_display = args_str

        args_label = QLabel("输入参数:")
        args_label.setStyleSheet("font-size: 11px; font-weight: bold; color: #888;")
        details_layout.addWidget(args_label)

        self.args_view = CompactTextBrowser(args_display, min_height=20, max_height=96)
        details_layout.addWidget(self.args_view)

        # Result section
        self.result_label = QLabel("执行结果:")
        self.result_label.setStyleSheet("font-size: 11px; font-weight: bold; color: #888; margin-top: 4px;")
        self.result_label.setVisible(False)
        details_layout.addWidget(self.result_label)

        self.result_view = MarkdownView("")
        self.result_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.result_view.setVisible(False)
        details_layout.addWidget(self.result_view)

        layout.addWidget(self.details_widget)

        if self.result_payload is not None:
            self.set_result(self.result_payload)

    def set_subtask(self, trace: dict):
        if not isinstance(trace, dict):
            return
        current = self.result_payload or normalize_tool_result('')
        result_payload = {
            'type': 'subtask_run',
            'content': str(trace.get('final_message') or trace.get('error') or trace.get('goal') or current.get('content') or ''),
            'summary': str(trace.get('final_message') or trace.get('error') or trace.get('goal') or current.get('summary') or '')[:220],
            'metadata': dict(current.get('metadata') or {}),
            'run': trace,
        }
        self.result_payload = result_payload
        if self.subtask_widget is None:
            self.subtask_widget = SubtaskRunWidget(trace)
            self.layout().addWidget(self.subtask_widget)
        else:
            self.subtask_widget.set_trace(trace)
        self._apply_result_header(result_payload)
    def _toggle(self):
        if self._tool_kind() in {'subagent', 'capability'}:
            if self.subtask_widget is not None:
                self.subtask_widget._toggle()
            return
        self.is_expanded = not self.is_expanded
        if self.details_widget is not None:
            self.details_widget.setVisible(self.is_expanded)

    def _tool_name(self) -> str:
        return _tool_call_name(self.tool_call)

    def _tool_kind(self, name: str | None = None) -> str:
        return _tool_call_kind(name or self._tool_name())

    def _display_name(self, name: str | None = None) -> str:
        return _tool_call_display_name(name or self._tool_name())

    def _kind_label(self, kind: str | None = None) -> str:
        return _tool_call_kind_label(kind or self._tool_kind())

    def _running_title(self, name: str) -> str:
        if name == 'ask_questions':
            return '等待你的选择'
        kind = self._tool_kind(name)
        return f'{self._kind_label(kind)}运行中 · {self._display_name(name)}'

    def _completed_title(self, name: str) -> str:
        if name == 'ask_questions':
            return '✓ 已完成: ask_questions'
        kind = self._tool_kind(name)
        return f'✓ {self._kind_label(kind)}已完成 · {self._display_name(name)}'

    def _result_summary(self, result: Any) -> str:
        if isinstance(result, dict):
            summary = str(result.get('summary') or '').strip()
            if summary:
                return _plain_summary(summary, 120)
            result = result.get('content') or result.get('final_message') or ''

        text = str(result or '').strip()
        if not text:
            return ''
        first_line = text.splitlines()[0].strip()
        if len(first_line) > 120:
            return first_line[:119] + '…'
        return first_line

    def _result_meta_hint(self) -> str:
        result = self.result_payload or normalize_tool_result('')
        metadata = result.get('metadata') or {}
        if not isinstance(metadata, dict):
            return ''
        result_file = str(metadata.get('tool_result_file') or '').strip()
        if result_file:
            return f'完整输出已写入文件：{result_file}'
        if metadata.get('tool_result_truncated'):
            return '结果过长，当前仅展示摘要或预览。'
        return ''

    def _apply_result_header(self, result: Any) -> None:
        name = self._tool_name()
        if self._tool_kind(name) in {'subagent', 'capability'}:
            return
        if self.toggle_btn is not None:
            self.toggle_btn.setText(self._completed_title(name))
        summary = self._result_summary(result)
        if self.summary_label is not None:
            self.summary_label.setVisible(bool(summary))
        if summary and self.summary_label is not None:
            self.summary_label.setText(f"摘要：{summary}")

    def _placeholder_subtask_trace(self, result_payload: dict | None = None) -> dict[str, Any]:
        payload = result_payload or {}
        kind = self._tool_kind()
        final_message = str(payload.get('content') or payload.get('summary') or '').strip()
        status = 'completed' if final_message else 'running'
        return {
            'id': str(self.tool_id or ''),
            'kind': kind,
            'name': self._tool_name(),
            'title': self._display_name(),
            'status': status,
            'messages': [],
            'final_message': final_message,
            'goal': final_message,
            'duration_ms': 0,
        }

    def set_result(self, result: Any):
        result_payload = normalize_tool_result(result)
        self.result_payload = result_payload
        if result_payload.get('type') == 'subtask_run':
            run = result_payload.get('run')
            if isinstance(run, dict):
                self.set_subtask(run)
            self._apply_result_header(result_payload)
            return
        if self._tool_kind() in {'subagent', 'capability'}:
            self.set_subtask(self._placeholder_subtask_trace(result_payload))
            return
        if self.result_label is not None:
            self.result_label.setVisible(True)
        if self.result_view is not None:
            self.result_view.setVisible(True)
            self.result_view.set_markdown(str(result_payload.get('content') or ''))

        name = self._tool_name()
        if self.toggle_btn is not None:
            self.toggle_btn.setText(self._completed_title(name))

        summary = self._result_summary(result_payload)
        if self.summary_label is not None:
            self.summary_label.setVisible(bool(summary))
        if summary and self.summary_label is not None:
            self.summary_label.setText(f"摘要：{summary}")

        meta_hint = self._result_meta_hint()
        if self.meta_label is not None:
            self.meta_label.setVisible(bool(meta_hint))
        if meta_hint and self.meta_label is not None:
            self.meta_label.setText(meta_hint)

    def update_content(self):
        """Refresh content from the normalized invocation state."""
        if self.result_payload is not None:
            self.set_result(self.result_payload)


class ToolCallsSection(QWidget):
    """Container for normalized tool invocation views."""

    def __init__(self, tool_calls: List[dict] | List[ToolInvocationView], parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.invocations: list[ToolInvocationView] = [item for item in tool_calls if isinstance(item, ToolInvocationView)]
        self.tool_calls = [item.tool_call if isinstance(item, ToolInvocationView) else item for item in tool_calls]
        self.items = {}
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(2)

        header = QLabel(self._header_text())
        header.setObjectName("message_badge")
        layout.addWidget(header)

        sources = self.invocations if self.invocations else self.tool_calls
        for source in sources:
            tool_call = source.tool_call if isinstance(source, ToolInvocationView) else source
            if not isinstance(tool_call, dict):
                continue
            item = ToolCallItem(source)
            self.items[tool_call.get('id')] = item
            layout.addWidget(item)

    def update_subtask(self, tool_id: str, trace: dict):
        if tool_id in self.items:
            self.items[tool_id].set_subtask(trace)

    def refresh_all(self):
        """Refresh all items from their underlying data"""
        for item in self.items.values():
            item.update_content()

    def _header_text(self) -> str:
        counts = {"tool": 0, "capability": 0, "subagent": 0}
        for tool_call in self.tool_calls:
            counts[_tool_call_kind(_tool_call_name(tool_call))] += 1
        parts = []
        if counts["tool"]:
            parts.append(f"工具 {counts['tool']}")
        if counts["capability"]:
            parts.append(f"能力 {counts['capability']}")
        if counts["subagent"]:
            parts.append(f"子 Agent {counts['subagent']}")
        detail = " / ".join(parts) if parts else "无调用"
        return f"调用链 ({len(self.tool_calls)}) · {detail}"


class SubtaskRunWidget(QWidget):
    """Collapsible child-agent run rendered from a normalized RunTree node."""

    def __init__(self, trace: dict[str, Any], parent=None):
        super().__init__(parent)
        self.trace = dict(trace or {})
        self.is_expanded = False
        self.toggle_btn = None
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

        self.toggle_btn = QToolButton()
        self.toggle_btn.setObjectName("tool_call_header")
        self.toggle_btn.setProperty("kind", "subagent")
        self.toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.toggle_btn.setMaximumHeight(22)
        self.toggle_btn.clicked.connect(self._toggle)
        layout.addWidget(self.toggle_btn)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        self.summary_label.setProperty("muted", True)
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
        self.trace = dict(trace or {})
        self._refresh_header()
        if self.is_expanded:
            self._rebuild_content()

    def _toggle(self):
        self.is_expanded = not self.is_expanded
        if self.content_widget is not None:
            self.content_widget.setVisible(self.is_expanded)
        if self.is_expanded:
            self._rebuild_content()
        else:
            self._clear_content()

    def _refresh_header(self):
        status = str(self.trace.get('status') or 'completed').strip().lower()
        title = str(self.trace.get('title') or self.trace.get('name') or 'Subagent').strip()
        kind = str(self.trace.get('kind') or 'subagent').strip().lower()
        kind_label = '能力' if kind == 'capability' else '子 Agent'
        messages = [m for m in self.trace.get('messages') or [] if isinstance(m, dict)]
        duration_ms = int(self.trace.get('duration_ms') or 0)
        duration = f" · {duration_ms / 1000:.1f}s" if duration_ms > 0 else ""

        if self.toggle_btn is not None:
            self.toggle_btn.setText(
                f"{_subtask_status_icon(status)} {kind_label} · {title} · {_subtask_status_label(status)} · 消息 {len(messages)}{duration}"
            )

        summary = _plain_summary(
            self.trace.get('final_message') or self.trace.get('error') or self.trace.get('goal'),
            limit=180,
        )
        if self.summary_label is not None:
            self.summary_label.setVisible(bool(summary))
            self.summary_label.setText(f"摘要：{summary}" if summary else "")

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

        goal = str(self.trace.get('goal') or '').strip()
        if goal:
            goal_label = QLabel(f"目标：{_plain_summary(goal, 240)}")
            goal_label.setWordWrap(True)
            goal_label.setProperty("muted", True)
            content_layout.addWidget(goal_label)

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
        for child_message in render_messages[:limit]:
            try:
                content_layout.addWidget(MessageWidget(child_message, embedded=True))
            except Exception as exc:
                logger.debug("Failed to render subtask message: %s", exc)
        if len(render_messages) > limit:
            self.show_more_btn = QPushButton(f"显示全部 {len(render_messages)} 条子任务消息（还有 {len(render_messages) - limit} 条）")
            self.show_more_btn.setProperty("secondary", True)
            self.show_more_btn.clicked.connect(self._show_all_messages)
            content_layout.addWidget(self.show_more_btn)

        error = str(self.trace.get('error') or '').strip()
        if error:
            error_label = QLabel("错误:")
            error_label.setStyleSheet("font-size: 11px; font-weight: bold; color: #c0392b; margin-top: 4px;")
            content_layout.addWidget(error_label)
            error_view = MarkdownView(error)
            error_view.set_height_adjustment(minimum_height=22, padding=2)
            content_layout.addWidget(error_view)
        self._content_built = True


SubagentTraceItem = SubtaskRunWidget


class InlineQuestionCard(QFrame):
    """Inline interactive card used by askQuestions as the primary UI path."""

    submitted = pyqtSignal(object)
    cancelled = pyqtSignal()

    def __init__(self, question: dict[str, Any], parent=None):
        super().__init__(parent)
        self.question = dict(question or {})
        self._option_controls: list[tuple[str, QWidget]] = []
        self._radio_group = QButtonGroup(self)
        self._radio_group.setExclusive(True)
        self._freeform_input: QLineEdit | None = None
        self._setup_ui()
        self._apply_recommended_defaults()

    def _setup_ui(self) -> None:
        self.setObjectName("message_widget")
        self.setProperty("role", "assistant")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)

        role_label = QLabel("助手")
        role_label.setObjectName("message_role")
        header.addWidget(role_label)

        badge = QLabel("需要选择")
        badge.setObjectName("message_badge")
        header.addWidget(badge)
        header.addStretch()
        layout.addLayout(header)

        title = str(self.question.get("header") or "需要你的选择").strip() or "需要你的选择"
        question_text = str(self.question.get("question") or "请选择一个选项").strip() or "请选择一个选项"
        message_text = str(self.question.get("message") or "").strip()

        title_label = QLabel(title)
        title_label.setObjectName("task_text")
        layout.addWidget(title_label)

        question_label = QLabel(question_text)
        question_label.setWordWrap(True)
        layout.addWidget(question_label)

        if message_text:
            message_label = QLabel(message_text)
            message_label.setProperty("muted", True)
            message_label.setWordWrap(True)
            layout.addWidget(message_label)

        for index, option in enumerate(self.question.get("options") or []):
            layout.addWidget(self._build_option_widget(option, index))

        if bool(self.question.get("allow_freeform_input", True)):
            freeform_title = QLabel("补充输入")
            freeform_title.setProperty("muted", True)
            layout.addWidget(freeform_title)

            self._freeform_input = QLineEdit()
            self._freeform_input.setPlaceholderText("可选：输入补充说明…")
            layout.addWidget(self._freeform_input)

        self.validation_label = QLabel("")
        self.validation_label.setProperty("muted", True)
        self.validation_label.setWordWrap(True)
        self.validation_label.setStyleSheet("color: #c0392b;")
        self.validation_label.setVisible(False)
        layout.addWidget(self.validation_label)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.cancelled.emit)
        actions.addWidget(cancel_btn)

        submit_btn = QPushButton("提交")
        submit_btn.setProperty("primary", True)
        submit_btn.clicked.connect(self._submit)
        actions.addWidget(submit_btn)
        layout.addLayout(actions)

    def _build_option_widget(self, option: Any, index: int) -> QWidget:
        payload = option if isinstance(option, dict) else {"label": str(option or "").strip()}
        label = str(payload.get("label") or "").strip() or f"选项 {index + 1}"
        description = str(payload.get("description") or "").strip()
        multi_select = bool(self.question.get("multi_select", False))

        container = QFrame()
        container.setObjectName("task_card")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        if multi_select:
            control: QWidget = QCheckBox(label)
        else:
            control = QRadioButton(label)
            self._radio_group.addButton(control, index)
        layout.addWidget(control)

        if description:
            desc = QLabel(description)
            desc.setWordWrap(True)
            desc.setProperty("muted", True)
            desc_layout = QHBoxLayout()
            desc_layout.setContentsMargins(20 if not multi_select else 24, 0, 0, 0)
            desc_layout.addWidget(desc)
            layout.addLayout(desc_layout)

        self._option_controls.append((label, control))
        return container

    def _apply_recommended_defaults(self) -> None:
        options = list(self.question.get("options") or [])
        if not options:
            return

        recommended = [
            str(option.get("label") or "").strip()
            for option in options
            if isinstance(option, dict) and option.get("recommended")
        ]
        recommended = [item for item in recommended if item]
        multi_select = bool(self.question.get("multi_select", False))

        if not recommended and not multi_select and self._option_controls:
            recommended = [self._option_controls[0][0]]

        for label, control in self._option_controls:
            should_select = label in recommended
            if isinstance(control, (QCheckBox, QRadioButton)):
                control.setChecked(bool(should_select))
                if should_select and not multi_select:
                    break

    def _selected_labels(self) -> list[str]:
        selected: list[str] = []
        for label, control in self._option_controls:
            if isinstance(control, (QCheckBox, QRadioButton)) and control.isChecked():
                selected.append(label)
        return selected

    def get_answer(self) -> dict[str, Any]:
        free_text = (self._freeform_input.text() if self._freeform_input else "").strip() or None
        selected = self._selected_labels()
        return {
            "selected": selected,
            "freeText": free_text,
            "skipped": not bool(selected or free_text),
        }

    def _submit(self) -> None:
        answer = self.get_answer()
        if answer["selected"] or answer["freeText"] or not self._option_controls:
            self.validation_label.setVisible(False)
            self.submitted.emit(answer)
            return

        self.validation_label.setText("请先选择至少一个选项，或输入补充说明后再提交。")
        self.validation_label.setVisible(True)


class MessageWidget(QFrame):
    """Widget for displaying a single message - Compact responsive layout"""

    edit_requested = pyqtSignal(str)
    delete_requested = pyqtSignal(str)

    def __init__(self, message: Message, parent=None, *, embedded: bool = False):
        super().__init__(parent)
        message_view = view_model_for_message(message)
        self.message_view = message_view
        self.message = message_view.message if message_view is not None else Message.from_dict(message.to_dict())
        self.embedded = bool(embedded)
        self._setup_ui()

    def _setup_ui(self):
        is_user = self.message.role == 'user'

        # Themeable styling via QSS
        self.setObjectName("message_widget")
        self.setProperty("role", "user" if is_user else "assistant")

        # Never consume vertical slack from the scroll area; blank space belongs to the viewport.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 3, 7, 3)
        layout.setSpacing(1)

        # Header - compact
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

        # Model + timestamp (from metadata / created_at)
        self._add_model_badge(header)
        self._add_timestamp_badge(header)

        # Stats - compact badges
        if self.message.tokens:
            self._add_badge(header, f"T:{self.message.tokens}")

        if self.message.response_time_ms:
            self._add_badge(header, f"{self.message.response_time_ms / 1000:.1f}s")

        header.addStretch()

        # Keep action buttons tight and consistent with the nav toolbar.
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
        if (not is_user) and self.message.thinking:
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
            self.tool_calls_widget = ToolCallsSection(invocations)
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
        copy_btn = QToolButton()
        copy_btn.setIcon(Icons.get_muted(Icons.COPY, scale_factor=0.75))
        copy_btn.setIconSize(QSize(14, 14))
        copy_btn.setToolTip("复制原文")
        copy_btn.setFixedSize(MESSAGE_ACTION_SIZE, MESSAGE_ACTION_SIZE)
        copy_btn.setObjectName("msg_copy_btn")
        copy_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        copy_btn.clicked.connect(self._copy_original_content)
        self._copy_btn = copy_btn
        layout.addWidget(copy_btn)

        edit_btn = QToolButton()
        edit_btn.setIcon(Icons.get_muted(Icons.EDIT, scale_factor=0.75))
        edit_btn.setIconSize(QSize(14, 14))
        edit_btn.setToolTip("编辑")
        edit_btn.setFixedSize(MESSAGE_ACTION_SIZE, MESSAGE_ACTION_SIZE)
        edit_btn.setObjectName("msg_edit_btn")
        edit_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        edit_btn.clicked.connect(lambda: self.edit_requested.emit(self.message.id))
        layout.addWidget(edit_btn)

        delete_btn = QToolButton()
        delete_btn.setIcon(Icons.get_error(Icons.TRASH, scale_factor=0.75))
        delete_btn.setIconSize(QSize(14, 14))
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
