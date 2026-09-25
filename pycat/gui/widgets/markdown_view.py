"""Markdown rendering widgets shared by chat message surfaces."""

from __future__ import annotations

import logging
import math
import re
from functools import partial

from PyQt6.QtCore import QCoreApplication, QPoint, QSize, Qt, QThreadPool, QTimer, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QImage, QTextCursor, QTextDocument, QTextOption, QWheelEvent
from PyQt6.QtWidgets import QAbstractScrollArea, QFrame, QSizePolicy, QTextBrowser, QTextEdit

from pycat.gui.runtime.background_job import BackgroundJob
from pycat.gui.utils.message_images import image_source
from pycat.gui.utils.theme import resolve_accent, resolve_theme, theme_tokens
from pycat.gui.widgets.themed_line_edit import ThemedContextMenuMixin
from pycat.models.contracts.config import DEFAULT_ACCENT

try:
    import markdown
except ImportError:  # pragma: no cover - optional runtime dependency
    markdown = None


logger = logging.getLogger(__name__)

# Matches hex colors truncated mid-stream (e.g. "#E", "#4A9") that would
# otherwise reach QCssParser and log "Unknown color name" warnings while a
# streaming message renders partial inline HTML.
_TRUNCATED_HEX_COLOR_RE = re.compile(r"(color\s*:\s*)(#[0-9a-fA-F]{1,5})(?![0-9a-fA-F])")


def _sanitize_truncated_colors(css_text: str) -> str:
    """Drop hex color fragments that were cut off mid-stream.

    During streaming, ``set_markdown`` may be called with partial inline
    HTML whose ``color:#E...`` attribute has not fully arrived yet. Qt's CSS
    parser logs ``QCssParser::parseHexColor: Unknown color name '#E'`` for
    such fragments. Removing the incomplete declaration is safe: the next
    streaming chunk re-renders the whole document anyway.
    """

    def _drop(match: re.Match[str]) -> str:
        return match.group(1)

    return _TRUNCATED_HEX_COLOR_RE.sub(_drop, css_text)


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


_FENCE_LINE_RE = re.compile(r"^\s{0,3}(```|~~~)")
_LIST_ITEM_RE = re.compile(r"^\s{0,3}(?:[-+*]\s+|\d+[.)]\s+)")
_HTML_CODE_BLOCK_RE = re.compile(r"<pre><code(?P<attrs>[^>]*)>(?P<code>.*?)</code></pre>", re.DOTALL)


def prepare_markdown_html_for_qt(
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


def normalize_markdown_for_view(text: str) -> str:
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


def _load_markdown_image(loader):
    try:
        return loader, loader()
    except Exception as exc:
        logger.debug("Markdown image could not be loaded: %s", exc)
        return loader, QImage()


class MarkdownView(ThemedContextMenuMixin, QTextBrowser):
    """A compact, auto-height markdown-capable viewer."""

    image_clicked = pyqtSignal(str)
    image_menu_requested = pyqtSignal(str, QPoint)

    def __init__(self, text: str = "", parent=None, *, image_resources=None):
        super().__init__(parent)
        self._image_resources = image_resources
        self._image_values = {}
        self._image_jobs = {}
        self._image_urls = {}
        self._image_placeholder = QImage(16, 16, QImage.Format.Format_ARGB32_Premultiplied)
        self._image_placeholder.fill(Qt.GlobalColor.transparent)
        self._raw_markdown_text = ""
        self._rendered_theme = ""
        self._theme_timer = QTimer(self)
        self._theme_timer.setSingleShot(True)
        self._theme_timer.timeout.connect(self._refresh_theme)
        self._refit_timer = QTimer(self)
        self._refit_timer.setSingleShot(True)
        self._refit_timer.timeout.connect(self._finish_refit)
        self._pending_scroll_restore: tuple[int, bool] | None = None
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
        self.verticalScrollBar().rangeChanged.connect(self._on_scroll_range_changed)
        self.verticalScrollBar().actionTriggered.connect(self._cancel_scroll_restore)
        self.verticalScrollBar().sliderPressed.connect(self._cancel_scroll_restore)

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
        scrollbar = self.verticalScrollBar()
        reading_position = None
        if self._maximum_content_height is not None and self._raw_markdown_text:
            reading_position = (scrollbar.value(), scrollbar.value() >= scrollbar.maximum())
        if text is None:
            text = ""
        text = str(text)
        self._raw_markdown_text = text
        render_text = normalize_markdown_for_view(text)

        if markdown:
            try:
                extensions = ['fenced_code', 'tables', 'sane_lists']
                theme = resolve_theme(self)
                accent = resolve_accent(self)
                self._rendered_theme = f"{theme}:{accent}"
                html = markdown.markdown(render_text, extensions=extensions)
                html = prepare_markdown_html_for_qt(html, theme=theme, accent=accent)
                html = _sanitize_truncated_colors(html)
                self.setHtml(markdown_css(theme, accent) + html)
            except Exception:
                self.document().setMarkdown(render_text)
        else:
            try:
                self.document().setMarkdown(render_text)
            except Exception:
                self.setPlainText(text)

        self.refit_height()
        if reading_position is not None:
            position, follow_end = reading_position
            scrollbar.setValue(scrollbar.maximum() if follow_end else position)
        self._pending_scroll_restore = reading_position
        self._refit_timer.start(0)

    def _finish_refit(self) -> None:
        self.refit_height()
        if self._pending_scroll_restore is not None:
            position, follow_end = self._pending_scroll_restore
            self._pending_scroll_restore = None
            bar = self.verticalScrollBar()
            bar.setValue(bar.maximum() if follow_end else position)

    def _cancel_scroll_restore(self, *_args) -> None:
        self._pending_scroll_restore = None

    def _refresh_theme(self) -> None:
        self.set_markdown(self._raw_markdown_text)

    def loadResource(self, kind, url):
        if self._image_resources is None:
            return super().loadResource(kind, url)
        loader = self._image_resources.get(image_source(url.toString())) if kind == QTextDocument.ResourceType.ImageResource else None
        if loader is None:
            return self._image_placeholder
        self._image_urls.setdefault(loader, set()).add(url.toString())
        if loader in self._image_values:
            return self._image_values[loader]
        if loader not in self._image_jobs:
            job = BackgroundJob(partial(_load_markdown_image, loader))
            self._image_jobs[loader] = job
            # Qt disconnects this typed receiver on destruction. The worker
            # holds no widget or mutable conversation state.
            job.signals.finished.connect(self._image_loaded)
            QThreadPool.globalInstance().start(job)
        return self._image_placeholder

    @pyqtSlot(object, object)
    def _image_loaded(self, result, error):
        if result is None:
            return
        loader, image = result
        self._image_jobs.pop(loader, None)
        self._image_values[loader] = image
        for url in self._image_urls.get(loader, ()):
            self.document().addResource(QTextDocument.ResourceType.ImageResource, QUrl(url), image)
        self.document().markContentsDirty(0, self.document().characterCount())
        self.refit_height()
        self.viewport().update()

    def _fit_images(self, width):
        if not self._image_values:
            return
        changes = []
        block = self.document().begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                fragment = it.fragment()
                fmt = fragment.charFormat().toImageFormat()
                loader = self._image_resources.get(image_source(fmt.name())) if fmt.isValid() else None
                image = self._image_values.get(loader)
                if image is not None and not image.isNull():
                    size = image.size()
                    limit = QSize(max(1, width - 4), 480)
                    if size.width() > limit.width() or size.height() > limit.height():
                        size.scale(limit, Qt.AspectRatioMode.KeepAspectRatio)
                    if fmt.width() != size.width() or fmt.height() != size.height():
                        fmt.setWidth(size.width())
                        fmt.setHeight(size.height())
                        changes.append((fragment.position(), fragment.length(), fmt))
                it += 1
            block = block.next()
        for position, length, fmt in changes:
            cursor = QTextCursor(self.document())
            cursor.setPosition(position)
            cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
            cursor.setCharFormat(fmt)

    def mouseReleaseEvent(self, event):
        fmt = self.cursorForPosition(event.pos()).charFormat().toImageFormat()
        if event.button() == Qt.MouseButton.LeftButton and fmt.isValid() and not self.textCursor().hasSelection():
            if self._image_resources is not None and image_source(fmt.name()) in self._image_resources:
                self.image_clicked.emit(fmt.name())
                event.accept()
                return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        fmt = self.cursorForPosition(event.pos()).charFormat().toImageFormat()
        if fmt.isValid() and self._image_resources is not None and image_source(fmt.name()) in self._image_resources:
            self.image_menu_requested.emit(fmt.name(), event.globalPos())
            event.accept()
            return
        super().contextMenuEvent(event)

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
                self._theme_timer.start(0)
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

    def wheelEvent(self, event):
        self._cancel_scroll_restore()
        if self._maximum_content_height is None:
            # An auto-height reply belongs to the transcript's scroll area.
            parent = self.parentWidget()
            while parent is not None:
                if isinstance(parent, QAbstractScrollArea):
                    forwarded = QWheelEvent(
                        parent.viewport().mapFromGlobal(event.globalPosition()), event.globalPosition(),
                        event.pixelDelta(), event.angleDelta(), event.buttons(), event.modifiers(),
                        event.phase(), event.inverted(), device=event.pointingDevice(),
                    )
                    QCoreApplication.sendEvent(parent.viewport(), forwarded)
                    event.setAccepted(forwarded.isAccepted())
                    return
                parent = parent.parentWidget()
            event.ignore()
        else:
            super().wheelEvent(event)

    def _on_scroll_range_changed(self, _minimum: int, maximum: int) -> None:
        if self._maximum_content_height is None and maximum > 0:
            self.verticalScrollBar().setRange(0, 0)
        elif self._maximum_content_height is not None and not self._fitting_height:
            self._refit_timer.start(0)

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
            self._fit_images(width)
            self.document().setTextWidth(width)
            size = self.document().documentLayout().documentSize()
            desired_height = max(self._minimum_content_height, int(math.ceil(size.height())) + self._height_padding)
            height = desired_height
            if self._maximum_content_height is not None:
                height = min(self._maximum_content_height, desired_height)
            self.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAsNeeded
                if height < desired_height
                else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
            if self.height() != height:
                self.setFixedHeight(height)
            if self._maximum_content_height is None:
                # QTextEdit extrapolates scroll ranges during lazy layout. Even
                # after documentSize() completes the layout, a wrapped code block
                # can retain an oversized hidden range. The full document already
                # fits here; remove the estimate, including keyboard/selection scroll.
                self.verticalScrollBar().setRange(0, 0)
            else:
                # Qt can retain an estimated range after laying out wrapped
                # headings/code. Use the laid-out document, not that estimate,
                # so the last line is reachable without overscrolling it.
                self.document().setTextWidth(max(120, self.viewport().width()))
                content_height = math.ceil(self.document().documentLayout().documentSize().height())
                self.verticalScrollBar().setRange(0, max(0, content_height - self.viewport().height()))
                self.verticalScrollBar().setPageStep(self.viewport().height())
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
