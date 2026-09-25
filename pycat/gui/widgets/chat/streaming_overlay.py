"""Streaming overlay widget for the chat view.

Manages the temporary UI that shows while the LLM is generating a response,
including content buffering, thinking panel, and auto-scroll.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Optional

from PyQt6.QtCore import QObject, Qt, QTimer
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from PyQt6.QtWidgets import QScrollArea

from pycat.gui.utils.display_text import message_time
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.widgets.capsule import SingleLineLabel
from pycat.gui.widgets.markdown_view import MarkdownView
from pycat.gui.widgets.message_widget import MESSAGE_BADGE_HEIGHT, MESSAGE_HEADER_HEIGHT
from pycat.gui.widgets.tool_call_view import ThinkingSection

logger = logging.getLogger(__name__)


_RENDER_INTERVAL_MS = 400


class StreamingOverlay(QObject):
    """Manages the streaming response overlay within a ChatView.

    This is a *helper object*, not a QWidget — the host ChatView owns
    the actual Qt objects and layout.  StreamingOverlay encapsulates
    the creation / update / teardown lifecycle so that ChatView stays
    slim.

    Typical usage (inside ChatView):

        self._stream = StreamingOverlay(scroll_area=self.scroll_area)
        self._stream.start(model="gpt-4o", parent_layout=self.messages_layout)
        self._stream.append_content(token)
        self._stream.finish()
    """

    def __init__(self, *, scroll_area: QScrollArea | None, should_auto_scroll: Callable[[], bool] | None = None) -> None:
        super().__init__(scroll_area)
        self._scroll_area = scroll_area
        self._should_auto_scroll = should_auto_scroll

        # Widget references (created in ``start``, cleared in ``finish``)
        self._container: Optional[QFrame] = None
        self._header: QWidget | None = None
        self._status_label: QLabel | None = None
        self._content_label: Optional[MarkdownView] = None
        self._thinking_section: ThinkingSection | None = None

        # Text buffers
        self._text: str = ""
        self._thinking_text: str = ""

        # Buffered rendering (avoids UI freezing on fast token streams)
        self._pending_text: str = ""
        self._displayed_text: str = ""
        self._pending_thinking_text: str = ""
        self._displayed_thinking_text: str = ""
        self._render_timer = QTimer(self)
        self._render_timer.setInterval(_RENDER_INTERVAL_MS)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._process_buffer)
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.timeout.connect(self._scroll_to_bottom_if_allowed)
        self._geometry_timer = QTimer(self)
        self._geometry_timer.setSingleShot(True)
        self._geometry_timer.timeout.connect(self._update_geometry_and_scroll)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def active(self) -> bool:
        """``True`` while a streaming overlay is visible."""
        return self._content_label is not None

    def start(self, *, model: str, parent_layout: QVBoxLayout, insert_index: int | None = None) -> None:
        """Create and show the streaming overlay container."""
        if self._content_label is not None:
            return  # already active

        container = QFrame()
        container.setObjectName("streaming_container")
        container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # Match the completed assistant message header.
        self._status_label = SingleLineLabel(self.tr('正在生成…'))
        self._status_label.setObjectName('run_status_hint')
        self._status_label.setContentsMargins(0, 8, 0, 8)
        self._header = QWidget()
        header = QHBoxLayout(self._header)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        header.setAlignment(Qt.AlignmentFlag.AlignTop)

        avatar = QLabel()
        avatar.setPixmap(Icons.brand().pixmap(24, 24))
        header.addWidget(avatar)
        role_label = QLabel("PyCat")
        role_label.setObjectName("message_role")
        role_label.setToolTip(model)
        self._style_role_label(role_label)
        header.addWidget(role_label)

        started_at = datetime.now()
        ts_label = QLabel(message_time(started_at))
        ts_label.setToolTip(message_time(started_at, full=True))
        ts_label.setObjectName("message_badge")
        self._style_badge(ts_label)
        header.addWidget(ts_label)

        header.addStretch()
        layout.addWidget(self._header)

        # Thinking panel (collapsible, shown above content)
        self._thinking_section = ThinkingSection('')
        self._thinking_section.setVisible(False)
        layout.addWidget(self._thinking_section)

        # Main content area
        self._content_label = MarkdownView("")
        self._content_label.setObjectName("message_content")
        self._content_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self._content_label)
        layout.addWidget(self._status_label)

        # Reset buffers
        self._text = ""
        self._thinking_text = ""
        self._pending_text = ""
        self._displayed_text = ""
        self._pending_thinking_text = ""
        self._displayed_thinking_text = ""
        self._sync_content_visibility()

        self._container = container
        if insert_index is None:
            insert_index = max(0, parent_layout.count())
        parent_layout.insertWidget(max(0, int(insert_index)), container)
        self._scroll_timer.start(50)

    @staticmethod
    def _style_role_label(label: QLabel) -> None:
        label.setFixedHeight(MESSAGE_HEADER_HEIGHT)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    @staticmethod
    def _style_badge(label: QLabel) -> None:
        label.setFixedHeight(MESSAGE_BADGE_HEIGHT)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def finish(self) -> None:
        """Tear down the streaming overlay and free resources."""
        self._render_timer.stop()
        self._scroll_timer.stop()
        self._geometry_timer.stop()

        if self._container is not None:
            self._container.deleteLater()
            self._container = None

        self._content_label = None
        self._header = None
        self._status_label = None
        self._thinking_section = None
        self._text = ""
        self._thinking_text = ""
        self._pending_text = ""
        self._displayed_text = ""
        self._pending_thinking_text = ""
        self._displayed_thinking_text = ""

    # ------------------------------------------------------------------
    # Content updates
    # ------------------------------------------------------------------

    def set_status_hint(self, title: str, detail: str = '') -> None:
        """Display the prepared hint; translated text must never select a state."""
        if self._status_label is None:
            return
        label = ' '.join(str(title or '').split())[:80]
        self._status_label.setText(label or self.tr('正在生成…'))
        self._status_label.setToolTip(detail)

    def append_content(self, token: str) -> None:
        """Append visible content; actual UI update is batched by timer."""
        if self._content_label is not None and token is not None:
            self._text += str(token)
            self._pending_text = self._text
            self._schedule_render()

    def append_thinking(self, text: str) -> None:
        """Append thinking content (shown in collapsible panel)."""
        if self._thinking_section is None or not text:
            return

        self._thinking_text += str(text)
        self._pending_thinking_text = self._thinking_text
        self._schedule_render()

    def restore(self, visible_text: str = "", thinking_text: str = "") -> None:
        """Restore streaming state from cached buffers (conversation switch)."""
        if not self._content_label:
            return

        self._text = str(visible_text or "")
        self._content_label.set_markdown(self._text)
        self._pending_text = self._text
        self._displayed_text = self._text

        self._thinking_text = str(thinking_text or "")
        self._pending_thinking_text = self._thinking_text
        self._displayed_thinking_text = self._thinking_text
        if self._thinking_section is not None:
            self._thinking_section.set_content(self._thinking_text)
            self._thinking_section.set_expanded(bool(self._thinking_text))

        self._sync_content_visibility()
        self._scroll_timer.start(10)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _process_buffer(self, *, schedule_layout: bool = True) -> None:
        """Flush pending text to UI (called by render timer)."""
        if not self._content_label:
            return
        changed = False
        if self._pending_text != self._displayed_text:
            self._displayed_text = self._pending_text
            self._content_label.set_markdown(self._displayed_text)
            changed = True
        if (
            self._thinking_section is not None
            and self._pending_thinking_text != self._displayed_thinking_text
        ):
            first_thinking = not self._displayed_thinking_text
            self._displayed_thinking_text = self._pending_thinking_text
            self._thinking_section.set_content(self._displayed_thinking_text)
            if first_thinking:
                self._thinking_section.set_expanded(True)
            changed = True
        if changed:
            self._sync_content_visibility()
            if self._container is not None:
                self._container.updateGeometry()
            if schedule_layout:
                self._geometry_timer.start(10)

    def _schedule_render(self) -> None:
        if self._content_label is not None and not self._render_timer.isActive():
            self._render_timer.start()

    def _sync_content_visibility(self) -> None:
        has_text = bool(self._displayed_text.strip())
        has_thinking = bool(self._displayed_thinking_text.strip())
        if self._header is not None:
            self._header.setVisible(has_text or has_thinking)
        if self._content_label is not None:
            self._content_label.setVisible(has_text)
        if self._thinking_section is not None:
            self._thinking_section.setVisible(has_thinking)

    def _update_geometry_and_scroll(self) -> None:
        if self._content_label is not None:
            self._content_label.refit_height()
            self._content_label.updateGeometry()
        if self._thinking_section is not None and self._thinking_section.is_expanded:
            content = self._thinking_section.content_widget
            if content is not None:
                content.refit_height()
                content.updateGeometry()
        if self._container is not None:
            self._container.updateGeometry()
        self._scroll_to_bottom_if_allowed()

    def _scroll_to_bottom_if_allowed(self) -> None:
        if self._should_auto_scroll is not None and not self._should_auto_scroll():
            return
        self._scroll_to_bottom()

    def _scroll_to_bottom(self) -> None:
        if self._scroll_area is None:
            return
        scrollbar = self._scroll_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
