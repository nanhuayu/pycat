"""Streaming overlay widget for the chat view.

Manages the temporary UI that shows while the LLM is generating a response,
including content buffering, thinking panel, and auto-scroll.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

if TYPE_CHECKING:
    from PyQt6.QtWidgets import QScrollArea

from ui.widgets.message_widget import MarkdownView
from ui.widgets.message_widget import MESSAGE_BADGE_HEIGHT, MESSAGE_HEADER_HEIGHT

logger = logging.getLogger(__name__)


class StreamingOverlay:
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
        self._scroll_area = scroll_area
        self._should_auto_scroll = should_auto_scroll

        # Widget references (created in ``start``, cleared in ``finish``)
        self._container: Optional[QFrame] = None
        self._content_label: Optional[MarkdownView] = None
        self._thinking_label: Optional[MarkdownView] = None
        self._thinking_btn: Optional[QPushButton] = None
        self._thinking_expanded: bool = False

        # Text buffers
        self._text: str = ""
        self._thinking_text: str = ""

        # Buffered rendering (avoids UI freezing on fast token streams)
        self._pending_text: str = ""
        self._displayed_text: str = ""
        self._render_timer = QTimer()
        self._render_timer.setInterval(60)
        self._render_timer.timeout.connect(self._process_buffer)

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
        layout.setContentsMargins(7, 3, 7, 3)
        layout.setSpacing(1)

        # Header row: role + model + timestamp
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        header.setAlignment(Qt.AlignmentFlag.AlignTop)

        role_label = QLabel("助手")
        role_label.setObjectName("message_role")
        self._style_role_label(role_label)
        header.addWidget(role_label)

        if model:
            model_label = QLabel(model)
            model_label.setObjectName("message_badge")
            model_label.setToolTip(model)
            self._style_badge(model_label)
            header.addWidget(model_label)

        ts_label = QLabel(datetime.now().strftime("%m-%d %H:%M"))
        ts_label.setObjectName("message_badge")
        self._style_badge(ts_label)
        header.addWidget(ts_label)

        header.addStretch()
        layout.addLayout(header)

        # Thinking panel (collapsible, shown above content)
        self._thinking_btn = QPushButton("思考")
        self._thinking_btn.setObjectName("thinking_toggle")
        self._thinking_btn.setMaximumHeight(20)
        self._thinking_btn.setVisible(False)
        self._thinking_btn.clicked.connect(self._toggle_thinking)
        layout.addWidget(self._thinking_btn)

        self._thinking_label = MarkdownView("")
        self._thinking_label.setObjectName("thinking_content")
        self._thinking_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._thinking_label.setVisible(False)
        layout.addWidget(self._thinking_label)

        # Main content area
        self._content_label = MarkdownView("正在生成...")
        self._content_label.setObjectName("message_content")
        self._content_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self._content_label)

        # Reset buffers
        self._text = ""
        self._thinking_text = ""
        self._pending_text = ""
        self._displayed_text = ""
        self._thinking_expanded = False

        self._container = container
        if insert_index is None:
            insert_index = max(0, parent_layout.count())
        parent_layout.insertWidget(max(0, int(insert_index)), container)
        QTimer.singleShot(50, self._scroll_to_bottom_if_allowed)

        self._render_timer.start()

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

        # Flush final state
        if self._content_label and self._pending_text != self._displayed_text:
            self._content_label.set_markdown(self._pending_text)

        if self._container is not None:
            self._container.deleteLater()
            self._container = None

        self._content_label = None
        self._thinking_label = None
        self._thinking_btn = None
        self._thinking_expanded = False
        self._text = ""
        self._thinking_text = ""
        self._pending_text = ""
        self._displayed_text = ""

    # ------------------------------------------------------------------
    # Content updates
    # ------------------------------------------------------------------

    def append_content(self, token: str) -> None:
        """Append visible content; actual UI update is batched by timer."""
        if self._content_label is not None and token is not None:
            self._text += str(token)
            self._pending_text = self._text

    def append_thinking(self, text: str) -> None:
        """Append thinking content (shown in collapsible panel)."""
        if not self._thinking_label or not text:
            return

        self._thinking_text += str(text)
        self._thinking_label.set_markdown(self._thinking_text)

        if self._thinking_btn:
            self._thinking_btn.setVisible(True)

        # Auto-expand on first thinking token
        if not self._thinking_expanded:
            self._thinking_expanded = True
            self._thinking_label.setVisible(True)
            if self._thinking_btn:
                self._thinking_btn.setText("收起思考")

        QTimer.singleShot(10, self._scroll_to_bottom_if_allowed)

    def restore(self, visible_text: str = "", thinking_text: str = "") -> None:
        """Restore streaming state from cached buffers (conversation switch)."""
        if not self._content_label:
            return

        self._text = str(visible_text or "")
        self._content_label.set_markdown(self._text or "正在生成...")

        self._thinking_text = str(thinking_text or "")
        if self._thinking_label and self._thinking_btn:
            if self._thinking_text:
                self._thinking_label.set_markdown(self._thinking_text)
                self._thinking_btn.setVisible(True)
                self._thinking_expanded = True
                self._thinking_label.setVisible(True)
                self._thinking_btn.setText("收起思考")
            else:
                self._thinking_btn.setVisible(False)
                self._thinking_expanded = False
                self._thinking_label.setVisible(False)
                self._thinking_btn.setText("思考")

        QTimer.singleShot(10, self._scroll_to_bottom_if_allowed)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _process_buffer(self) -> None:
        """Flush pending text to UI (called by render timer)."""
        if not self._content_label:
            return
        if self._pending_text != self._displayed_text:
            self._displayed_text = self._pending_text
            self._content_label.set_markdown(self._displayed_text or "正在生成...")
            if self._container is not None:
                self._container.updateGeometry()
            QTimer.singleShot(10, self._update_geometry_and_scroll)

    def _update_geometry_and_scroll(self) -> None:
        if self._content_label is not None:
            self._content_label.refit_height()
            self._content_label.updateGeometry()
        if self._thinking_label is not None:
            self._thinking_label.refit_height()
            self._thinking_label.updateGeometry()
        if self._container is not None:
            self._container.updateGeometry()
        self._scroll_to_bottom_if_allowed()

    def _toggle_thinking(self) -> None:
        if not self._thinking_label or not self._thinking_btn:
            return
        self._thinking_expanded = not self._thinking_expanded
        self._thinking_label.setVisible(self._thinking_expanded)
        self._thinking_btn.setText("收起思考" if self._thinking_expanded else "思考")

    def _scroll_to_bottom_if_allowed(self) -> None:
        if self._should_auto_scroll is not None and not self._should_auto_scroll():
            return
        self._scroll_to_bottom()

    def _scroll_to_bottom(self) -> None:
        if self._scroll_area is None:
            return
        scrollbar = self._scroll_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
