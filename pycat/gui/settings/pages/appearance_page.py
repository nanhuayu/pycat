from __future__ import annotations

from PyQt6.QtCore import QCoreApplication, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QComboBox, QLabel, QVBoxLayout, QWidget

from pycat.gui.settings.page_header import build_page_header
from pycat.gui.utils.combo_box import configure_combo_popup
from pycat.gui.utils.form_builder import FormSection
from pycat.gui.utils.theme import normalize_accent, theme_tokens
from pycat.models.contracts.config import DEFAULT_ACCENT


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
        language: str = "zh_CN",
        accent: str = DEFAULT_ACCENT,
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
            language,
            accent,
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
        language: str,
        accent: str,
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
            layout.addWidget(build_page_header(QCoreApplication.translate('AppearancePage', "通用"), QCoreApplication.translate('AppearancePage', "设置主题、输入与显示偏好。")))

        t = (theme or "light").lower()
        interface = FormSection(QCoreApplication.translate('AppearancePage', "界面"))
        self.language_combo = QComboBox()
        # Language names are autonyms so users can always switch back.
        self.language_combo.addItem("简体中文", "zh_CN")
        self.language_combo.addItem("English (Preview)", "en")
        self.language_combo.setCurrentIndex(1 if language == "en" else 0)
        configure_combo_popup(self.language_combo)
        interface.form.addRow(QCoreApplication.translate('AppearancePage', "语言"), self.language_combo)
        language_hint = QLabel(QCoreApplication.translate('AppearancePage', "保存并重启后生效。英文预览覆盖主界面、常用设置与运行检查，部分页面仍为中文。"))
        language_hint.setWordWrap(True)
        language_hint.setProperty("muted", True)
        interface.form.addRow(language_hint, info=True)
        self.theme_combo = interface.add_combo(
            QCoreApplication.translate('AppearancePage', "主题"),
            items=[QCoreApplication.translate('AppearancePage', "浅色"), QCoreApplication.translate('AppearancePage', "深色")],
            current_index=1 if t == "dark" else 0,
        )
        self.accent_combo = QComboBox()
        for value, label in (("lavender", QCoreApplication.translate('AppearancePage', "薰衣草紫")), ("blue", QCoreApplication.translate('AppearancePage', "海水蓝")), ("forest", QCoreApplication.translate('AppearancePage', "森林绿"))):
            color = theme_tokens("light", value).color("primary")
            self.accent_combo.addItem(_color_swatch(color), label, value)
        accent_index = self.accent_combo.findData(normalize_accent(accent))
        self.accent_combo.setCurrentIndex(accent_index if accent_index >= 0 else 0)
        configure_combo_popup(self.accent_combo)
        interface.form.addRow(QCoreApplication.translate('AppearancePage', "强调色"), self.accent_combo)
        self.thinking_check = interface.add_checkbox(
            QCoreApplication.translate('AppearancePage', "新会话默认显示推理过程"),
            checked=bool(show_thinking),
        )
        self.close_to_tray_check = interface.add_checkbox(
            QCoreApplication.translate('AppearancePage', "关闭窗口时最小化到系统托盘"),
            checked=bool(close_to_tray),
        )
        layout.addWidget(interface.group)

        runtime = FormSection(QCoreApplication.translate('AppearancePage', "网络与诊断"))
        self.proxy_edit = runtime.add_line_edit(
            QCoreApplication.translate('AppearancePage', "代理服务器"),
            text=proxy_url or "",
            placeholder="http://127.0.0.1:7890",
        )
        self.timeout_spin = runtime.add_double_spin(
            QCoreApplication.translate('AppearancePage', "模型超时 (秒)"),
            value=float(llm_timeout_seconds or 600.0),
            range=(30.0, 3600.0),
            step=30.0,
            decimals=0,
            tooltip=QCoreApplication.translate('AppearancePage', "模型请求的总超时，包括首包等待、流式响应和长输出。"),
        )
        self.log_stream_check = runtime.add_checkbox(QCoreApplication.translate('AppearancePage', "保存完整调试载荷"), checked=bool(log_stream))
        layout.addWidget(runtime.group)
        self.network_group = runtime.group

        layout.addStretch()

    def collect(self) -> dict:
        return {
            "language": str(self.language_combo.currentData()),
            "proxy_url": (self.proxy_edit.text() or "").strip(),
            "llm_timeout_seconds": float(self.timeout_spin.value()),
            "theme": "dark" if self.theme_combo.currentIndex() == 1 else "light",
            "accent": str(self.accent_combo.currentData() or DEFAULT_ACCENT),
            "show_thinking": bool(self.thinking_check.isChecked()),
            "close_to_tray": bool(self.close_to_tray_check.isChecked()),
            "log_stream": bool(self.log_stream_check.isChecked()),
        }
