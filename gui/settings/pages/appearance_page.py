from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QComboBox, QVBoxLayout, QWidget

from models.contracts.config import DEFAULT_ACCENT
from gui.settings.page_header import build_page_header
from gui.utils.combo_box import configure_combo_popup
from gui.utils.form_builder import FormSection
from gui.utils.theme import normalize_accent, theme_tokens


def _color_swatch(color: str) -> QIcon:
    pixmap = QPixmap(16, 16)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    painter.drawEllipse(2, 2, 12, 12)
    painter.end()
    return QIcon(pixmap)


class AppearancePage(QWidget):
    page_title = "通用"

    def __init__(
        self,
        *,
        theme: str = "light",
        accent: str = DEFAULT_ACCENT,
        show_stats: bool = False,
        show_thinking: bool = True,
        close_to_tray: bool = True,
        log_stream: bool = False,
        proxy_url: str = "",
        llm_timeout_seconds: float = 600.0,
        embedded: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self._setup_ui(
            theme,
            accent,
            show_stats,
            show_thinking,
            close_to_tray,
            log_stream,
            proxy_url,
            llm_timeout_seconds,
            embedded,
        )

    def _setup_ui(
        self,
        theme: str,
        accent: str,
        show_stats: bool,
        show_thinking: bool,
        close_to_tray: bool,
        log_stream: bool,
        proxy_url: str,
        llm_timeout_seconds: float,
        embedded: bool,
    ) -> None:
        layout = QVBoxLayout(self)
        margin = 12 if embedded else 16
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.setSpacing(12)

        if not embedded:
            layout.addWidget(build_page_header("通用", "网络、主题、显示与诊断选项统一在这里配置。"))

        t = (theme or "light").lower()
        interface = FormSection("界面")
        self.theme_combo = interface.add_combo(
            "主题",
            items=["浅色", "深色"],
            current_index=1 if t == "dark" else 0,
        )
        self.accent_combo = QComboBox()
        for value, label in (("lavender", "薰衣草紫"), ("blue", "Python 蓝")):
            color = theme_tokens("light", value).color("primary")
            self.accent_combo.addItem(_color_swatch(color), label, value)
        accent_index = self.accent_combo.findData(normalize_accent(accent))
        self.accent_combo.setCurrentIndex(accent_index if accent_index >= 0 else 0)
        configure_combo_popup(self.accent_combo)
        interface.form.addRow("强调色", self.accent_combo)
        self.stats_check = interface.add_checkbox("显示右侧辅助面板", checked=bool(show_stats))
        self.thinking_check = interface.add_checkbox("显示思考过程", checked=bool(show_thinking))
        self.close_to_tray_check = interface.add_checkbox(
            "关闭窗口时最小化到系统托盘",
            checked=bool(close_to_tray),
        )
        layout.addWidget(interface.group)

        runtime = FormSection("网络与诊断")
        self.proxy_edit = runtime.add_line_edit(
            "代理服务器",
            text=proxy_url or "",
            placeholder="http://127.0.0.1:7890",
        )
        self.timeout_spin = runtime.add_double_spin(
            "模型超时 (秒)",
            value=float(llm_timeout_seconds or 600.0),
            range=(30.0, 3600.0),
            step=30.0,
            decimals=0,
            tooltip="模型请求的总超时，包括首包等待、流式响应和长输出。",
        )
        self.log_stream_check = runtime.add_checkbox("记录流式调试日志", checked=bool(log_stream))
        layout.addWidget(runtime.group)

        layout.addStretch()

    def collect(self) -> dict:
        return {
            "proxy_url": (self.proxy_edit.text() or "").strip(),
            "llm_timeout_seconds": float(self.timeout_spin.value()),
            "theme": "dark" if self.theme_combo.currentIndex() == 1 else "light",
            "accent": str(self.accent_combo.currentData() or DEFAULT_ACCENT),
            "show_stats": bool(self.stats_check.isChecked()),
            "show_thinking": bool(self.thinking_check.isChecked()),
            "close_to_tray": bool(self.close_to_tray_check.isChecked()),
            "log_stream": bool(self.log_stream_check.isChecked()),
        }
