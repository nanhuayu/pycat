"""Markdown rendering widgets shared by chat message surfaces."""

from __future__ import annotations

import logging
import math
import re

from PyQt6.QtCore import QSize, QTimer, Qt
from PyQt6.QtGui import QTextOption
from PyQt6.QtWidgets import QAbstractScrollArea, QFrame, QSizePolicy, QTextBrowser, QTextEdit

from models.contracts.config import DEFAULT_ACCENT
from gui.utils.theme import resolve_accent, resolve_theme, theme_tokens

try:
    import markdown
except ImportError:  # pragma: no cover - optional runtime dependency
    markdown = None


logger = logging.getLogger(__name__)

def markdown_css(theme: object = "light", accent: object = DEFAULT_ACCENT) -> str:
    tokens = theme_tokens(theme, accent)
    markdown_code_bg = tokens.color("markdown_code_bg")
    markdown_code_text = tokens.color("markdown_code_text")
    markdown_quote = tokens.color("markdown_quote")
    markdown_link = tokens.color("markdown_link")
    selection_bg = tokens.color("text_selection")
    selection_text = tokens.color("text_selection_text")
    border = tokens.color("border")
    surface_alt = tokens.color("surface_alt")
    quote_background = tokens.color("primary_hover")

    return f"""
<style>
    body {{
        margin: 0;
        padding: 0;
        font-family: "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
        font-size: 13px;
        line-height: 1.55;
        overflow-wrap: anywhere;
        word-break: break-word;
    }}
    p {{
        margin: 0 0 6px 0;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        word-break: break-word;
    }}
    li, td {{
        overflow-wrap: anywhere;
        word-break: break-word;
    }}
    ul, ol {{
        margin-top: 3px;
        margin-bottom: 7px;
        padding-left: 20px;
    }}
    li {{
        margin-top: 0;
        margin-bottom: 3px;
    }}
    h1, h2, h3, h4, h5, h6 {{
        margin-top: 12px; margin-bottom: 6px;
        font-weight: 600;
    }}
    h1 {{ font-size: 19px; }}
    h2 {{ font-size: 17px; }}
    h3 {{ font-size: 15px; }}
    h4, h5, h6 {{ font-size: 14px; }}
    pre {{
        background-color: {markdown_code_bg};
        color: {markdown_code_text};
        border: none;
        padding: 10px 12px;
        border-radius: 6px;
        margin: 8px 0 10px 0;
        max-width: 100%;
        white-space: pre-wrap;
        word-wrap: break-word;
        overflow-wrap: anywhere;
        line-height: 1.0;
    }}
    code {{
        background-color: {markdown_code_bg};
        color: {markdown_code_text};
        border: none;
        padding: 1px 4px;
        border-radius: 5px;
        font-family: "Cascadia Mono", "Consolas", "Microsoft YaHei UI", monospace;
        font-size: 12px;
        white-space: pre-wrap;
        word-wrap: break-word;
        overflow-wrap: anywhere;
        word-break: break-word;
        line-height: 1.1;
    }}
    pre code {{
        background-color: transparent;
        border: none;
        padding: 0;
        border-radius: 0;
        white-space: pre-wrap;
        font-size: 12px;
        line-height: 1.0;
    }}
    table {{
        border-collapse: collapse;
        width: 100%;
        margin: 8px 0;
        border: 1px solid {border};
    }}
    th {{
        background-color: {surface_alt};
        font-weight: 700;
        padding: 6px;
        border: 1px solid {border};
        text-align: left;
    }}
    td {{
        padding: 6px;
        border: 1px solid {border};
    }}
    blockquote {{
        border-left: 4px solid {markdown_quote};
        background-color: {quote_background};
        padding: 8px 10px;
        margin: 8px 0;
        color: inherit;
        border-radius: 0 8px 8px 0;
    }}
    blockquote p {{ margin-bottom: 4px; }}
    ::selection {{
        background-color: {selection_bg};
        color: {selection_text};
    }}
    a {{ color: {markdown_link}; text-decoration: none; }}
</style>
"""


MARKDOWN_CODE_BG = theme_tokens("light").color("markdown_code_bg")
MARKDOWN_CSS = markdown_css("light")

_FENCE_LINE_RE = re.compile(r"^\s{0,3}(```|~~~)")
_LIST_ITEM_RE = re.compile(r"^\s{0,3}(?:[-+*]\s+|\d+[.)]\s+)")
_HTML_CODE_BLOCK_RE = re.compile(r"<pre><code(?P<attrs>[^>]*)>(?P<code>.*?)</code></pre>", re.DOTALL)


def _prepare_markdown_html_for_qt(
    html: str,
    *,
    theme: object = "light",
    accent: object = DEFAULT_ACCENT,
) -> str:
    """Make fenced code blocks render as stable Qt rich-text blocks."""

    tokens = theme_tokens(theme, accent)
    markdown_code_bg = tokens.color("markdown_code_bg")
    markdown_code_text = tokens.color("markdown_code_text")

    def _replace_code_block(match: re.Match[str]) -> str:
        code_html = match.group("code").rstrip("\n")
        return (
            '<pre class="qt-code-pre" '
            f'style="background-color:{markdown_code_bg}; color:{markdown_code_text}; '
            'border:0; border-style:none; '
            'border-width:0; padding:10px 12px; margin:8px 0 10px 0; '
            'border-radius:6px; line-height:1.0; white-space:pre-wrap; '
            'font-family:&quot;Cascadia Mono&quot;, &quot;Consolas&quot;, &quot;Microsoft YaHei UI&quot;, monospace; '
            'font-size:12px;">'
            f"{code_html}"
            "</pre>"
        )

    return _HTML_CODE_BLOCK_RE.sub(_replace_code_block, str(html or ""))


def _normalize_markdown_for_view(text: str) -> str:
    """Normalize LLM-flavored markdown so QTextDocument renders lists reliably."""
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


class MarkdownView(QTextBrowser):
    """A compact, auto-height markdown-capable viewer."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._raw_markdown_text = ""
        self._rendered_theme = ""
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setOpenExternalLinks(True)
        self.setTextInteractionFlags(
            self.textInteractionFlags()
            | Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        # Mouse selection should keep keyboard ownership so Ctrl+C/Ctrl+A work.
        # ClickFocus avoids adding every message body to the Tab focus chain.
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self._fitting_height = False
        self._minimum_content_height = 14
        self._maximum_content_height: int | None = None
        self._height_padding = 2

        doc = self.document()
        opt = doc.defaultTextOption()
        opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        doc.setDefaultTextOption(opt)
        doc.setDocumentMargin(0)
        try:
            self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        except Exception:
            pass

        try:
            doc.documentLayout().documentSizeChanged.connect(self._on_document_size_changed)
        except Exception as exc:
            logger.debug("Failed to connect markdown document size listener: %s", exc)

        self.set_markdown(text)

    def set_markdown(self, text: str) -> None:
        if text is None:
            text = ""
        text = str(text)
        self._raw_markdown_text = text
        render_text = _normalize_markdown_for_view(text)

        if markdown:
            try:
                extensions = ['fenced_code', 'tables', 'sane_lists']
                theme = resolve_theme(self)
                accent = resolve_accent(self)
                self._rendered_theme = f"{theme}:{accent}"
                html = markdown.markdown(render_text, extensions=extensions)
                html = _prepare_markdown_html_for_qt(html, theme=theme, accent=accent)
                self.setHtml(markdown_css(theme, accent) + html)
            except Exception:
                self.document().setMarkdown(render_text)
        else:
            try:
                self.document().setMarkdown(render_text)
            except Exception:
                self.setPlainText(text)

        self.refit_height()
        QTimer.singleShot(0, self.refit_height)

    def event(self, event):
        result = super().event(event)
        if event.type() in {
            event.Type.PaletteChange,
            event.Type.ParentChange,
            event.Type.StyleChange,
            event.Type.Polish,
        }:
            theme = resolve_theme(self)
            accent = resolve_accent(self)
            if self._raw_markdown_text and f"{theme}:{accent}" != self._rendered_theme:
                QTimer.singleShot(0, lambda: self.set_markdown(self._raw_markdown_text))
        return result

    def set_height_adjustment(
        self,
        *,
        minimum_height: int = 14,
        padding: int = 2,
        maximum_height: int | None = None,
    ) -> None:
        self._minimum_content_height = max(1, int(minimum_height))
        self._height_padding = max(0, int(padding))
        self._maximum_content_height = max(self._minimum_content_height, int(maximum_height)) if maximum_height else None
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
            desired_height = max(self._minimum_content_height, int(math.ceil(size.height())) + self._height_padding)
            height = desired_height
            if self._maximum_content_height is not None:
                height = min(self._maximum_content_height, desired_height)
                self.setVerticalScrollBarPolicy(
                    Qt.ScrollBarPolicy.ScrollBarAsNeeded
                    if desired_height > self._maximum_content_height
                    else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
                )
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
