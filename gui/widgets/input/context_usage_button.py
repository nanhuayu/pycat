"""Compact context budget indicator for the composer toolbar."""
from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QToolButton

from core.llm.token_budget import TokenUsageSnapshot, format_token_count
from gui.utils.theme import resolve_accent, resolve_theme, theme_tokens


class ContextUsageButton(QToolButton):
    """Render the current token budget and request the existing compact action."""

    compact_requested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("context_usage_btn")
        self.setFixedSize(30, 30)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("上下文占用")
        self._snapshot: TokenUsageSnapshot | None = None
        self._streaming = False
        self._busy = False
        self.clicked.connect(self._request_compact)
        self._refresh_state()

    def set_snapshot(self, snapshot: TokenUsageSnapshot | None) -> None:
        self._snapshot = snapshot
        self._refresh_state()

    def set_streaming_state(self, streaming: bool) -> None:
        self._streaming = bool(streaming)
        self._refresh_state()

    def set_busy_state(self, busy: bool) -> None:
        self._busy = bool(busy)
        self._refresh_state()

    def _request_compact(self) -> None:
        if self.isEnabled():
            self.compact_requested.emit()

    def _refresh_state(self) -> None:
        snapshot = self._snapshot
        has_messages = bool(snapshot is not None and snapshot.active_messages > 0)
        self.setEnabled(
            bool(
                has_messages
                and not self._streaming
                and not self._busy
            )
        )
        if self._busy:
            self.setProperty("status", "compact")
            self.setToolTip("正在压缩上下文…")
        elif snapshot is None:
            self.setProperty("status", "empty")
            self.setToolTip("上下文占用：暂无消息")
        else:
            status = str(snapshot.status or "ok")
            self.setProperty("status", status)
            self.setToolTip(self._tooltip(snapshot))
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    @staticmethod
    def _tooltip(snapshot: TokenUsageSnapshot) -> str:
        limit = snapshot.effective_prompt_limit or snapshot.context_window
        threshold = snapshot.compact_threshold_tokens
        threshold_ratio = (threshold / limit * 100.0) if limit > 0 else 0.0
        lines = [
            f"上下文占用：{snapshot.usage_ratio * 100:.1f}%",
            f"已用输入：{format_token_count(snapshot.context_tokens)}",
            f"模型窗口：{format_token_count(snapshot.context_window)}",
            f"本次输出上限：{format_token_count(snapshot.output_limit)}",
            f"可用输入：{format_token_count(limit)}",
            f"剩余输入：{format_token_count(snapshot.remaining_prompt_tokens)}",
            f"自动压缩：{format_token_count(threshold)}（可用输入的 {threshold_ratio:.0f}%）",
            "点击压缩上下文 (/compact)",
        ]
        if snapshot.budget.model_id:
            lines.append(f"模型：{snapshot.budget.model_id}")
        if snapshot.status != "ok":
            lines.append(f"状态：{snapshot.status}")
        return "\n".join(lines)

    def _status_color(self, tokens) -> str:
        snapshot = self._snapshot
        if snapshot is None:
            return tokens.color("muted")
        return {
            "danger": tokens.color("error"),
            "compact": tokens.color("warning"),
            "warning": tokens.color("warning"),
        }.get(str(snapshot.status or "ok"), tokens.color("primary"))

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(6.0, 6.0, 18.0, 18.0)
        tokens = theme_tokens(resolve_theme(self), resolve_accent(self))
        base_pen = QPen(QColor(tokens.color("border")), 2.0)
        base_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(base_pen)
        painter.drawArc(rect, 0, 360 * 16)

        snapshot = self._snapshot
        if snapshot is not None:
            ratio = 0.75 if self._busy else max(0.0, min(1.0, float(snapshot.usage_ratio or 0.0)))
            progress_pen = QPen(QColor(self._status_color(tokens)), 2.5)
            progress_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(progress_pen)
            if ratio > 0:
                painter.drawArc(rect, 90 * 16, -int(round(ratio * 360 * 16)))
            painter.setPen(QPen(QColor(tokens.color("text"))))
            font = painter.font()
            font.setPixelSize(8)
            painter.setFont(font)
            painter.drawText(
                rect,
                Qt.AlignmentFlag.AlignCenter,
                "…" if self._busy else str(round(ratio * 100)),
            )
        painter.end()
