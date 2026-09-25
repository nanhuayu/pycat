"""Compact context budget indicator for the composer toolbar."""
from __future__ import annotations

from PyQt6.QtCore import QCoreApplication, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QToolButton

from pycat.core.llm.token_budget import TokenUsageSnapshot, format_token_count
from pycat.gui.utils.theme import resolve_accent, resolve_theme, theme_tokens


class ContextUsageButton(QToolButton):
    """Render the current token budget and request the existing compact action."""

    compact_requested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("context_usage_btn")
        self.setFixedSize(106, 24)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(QCoreApplication.translate('ContextUsageButton', "上下文占用"))
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
            self.setToolTip(QCoreApplication.translate('ContextUsageButton', "正在压缩上下文…"))
        elif snapshot is None:
            self.setProperty("status", "empty")
            self.setToolTip(QCoreApplication.translate('ContextUsageButton', "上下文占用：暂无消息"))
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
            QCoreApplication.translate('ContextUsageButton', '上下文占用：{value:.1f}%').format(value=snapshot.usage_ratio * 100),
            QCoreApplication.translate('ContextUsageButton', '已用输入：{value}').format(value=format_token_count(snapshot.context_tokens)),
            QCoreApplication.translate('ContextUsageButton', '模型窗口：{value}').format(value=format_token_count(snapshot.context_window)),
            QCoreApplication.translate('ContextUsageButton', '本次输出上限：{value}').format(value=format_token_count(snapshot.output_limit)),
            QCoreApplication.translate('ContextUsageButton', '可用输入：{value}').format(value=format_token_count(limit)),
            QCoreApplication.translate('ContextUsageButton', '剩余输入：{value}').format(value=format_token_count(snapshot.remaining_prompt_tokens)),
            QCoreApplication.translate('ContextUsageButton', '自动压缩：{value}（可用输入的 {threshold_ratio:.0f}%）').format(value=format_token_count(threshold), threshold_ratio=threshold_ratio),
        ]
        if snapshot.source == "provider_request":
            pressure = QCoreApplication.translate('ContextUsageButton', "紧张") if snapshot.replay_pressure == "tight" else QCoreApplication.translate('ContextUsageButton', "充裕")
            lines.append(QCoreApplication.translate('ContextUsageButton', '口径：最近一次实际请求体估算（{pressure}视图）').format(pressure=pressure))
        else:
            lines.append(QCoreApplication.translate('ContextUsageButton', "口径：当前会话粗略估算（发送前会按实际请求重算）"))
        lines.append(QCoreApplication.translate('ContextUsageButton', "点击压缩上下文 (/compact)"))
        if snapshot.budget.model_id:
            lines.append(QCoreApplication.translate('ContextUsageButton', '模型：{model_id}').format(model_id=snapshot.budget.model_id))
        if snapshot.status != "ok":
            lines.append(QCoreApplication.translate('ContextUsageButton', '状态：{status}').format(status=snapshot.status))
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
        rect = QRectF(4.0, 5.0, 14.0, 14.0)
        tokens = theme_tokens(resolve_theme(self), resolve_accent(self))
        base_pen = QPen(QColor(tokens.color("border")), 2.0)
        base_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(base_pen)
        painter.drawArc(rect, 0, 360 * 16)

        snapshot = self._snapshot
        if snapshot is not None:
            ratio = max(0.0, min(1.0, float(snapshot.usage_ratio or 0.0)))
            progress_pen = QPen(QColor(self._status_color(tokens)), 2.5)
            progress_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(progress_pen)
            if ratio > 0:
                painter.drawArc(rect, 90 * 16, -int(round(ratio * 360 * 16)))
            painter.setPen(QPen(QColor(tokens.color("text"))))
            font = painter.font()
            font.setPixelSize(11)
            painter.setFont(font)
            painter.drawText(
                QRectF(24, 0, 82, 24),
                Qt.AlignmentFlag.AlignVCenter,
                QCoreApplication.translate('ContextUsageButton', "正在压缩…") if self._busy else QCoreApplication.translate('ContextUsageButton', '上下文 {value}%').format(value=round(ratio * 100)),
            )
        else:
            painter.setPen(QColor(tokens.color("muted")))
            font = painter.font()
            font.setPixelSize(11)
            painter.setFont(font)
            painter.drawText(QRectF(24, 0, 82, 24), Qt.AlignmentFlag.AlignVCenter, QCoreApplication.translate('ContextUsageButton', "上下文 0%"))
        painter.end()
