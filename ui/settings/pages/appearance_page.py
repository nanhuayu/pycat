from __future__ import annotations

from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QGroupBox,
    QFormLayout,
    QComboBox,
    QVBoxLayout as QVBox,
    QCheckBox,
    QLineEdit,
    QLabel,
    QDoubleSpinBox,
)

from ui.settings.page_header import build_page_header
from ui.utils.combo_box import configure_combo_popup


class AppearancePage(QWidget):
    page_title = "通用"

    def __init__(
        self,
        *,
        theme: str = "light",
        show_stats: bool = True,
        show_thinking: bool = True,
        log_stream: bool = False,
        proxy_url: str = "",
        llm_timeout_seconds: float = 600.0,
        parent=None,
    ):
        super().__init__(parent)
        self._log_stream = bool(log_stream)
        self._setup_ui(theme, show_stats, show_thinking, log_stream, proxy_url, llm_timeout_seconds)

    def _setup_ui(
        self,
        theme: str,
        show_stats: bool,
        show_thinking: bool,
        log_stream: bool,
        proxy_url: str,
        llm_timeout_seconds: float,
    ) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(build_page_header("通用", "网络、主题、显示与诊断选项统一在这里配置。"))

        network_group = QGroupBox("网络与请求")
        network_layout = QFormLayout(network_group)

        self.proxy_edit = QLineEdit()
        self.proxy_edit.setText(proxy_url or "")
        self.proxy_edit.setPlaceholderText("http://127.0.0.1:7890")
        network_layout.addRow("代理服务器:", self.proxy_edit)

        self.timeout_spin = QDoubleSpinBox()
        self.timeout_spin.setRange(30.0, 3600.0)
        self.timeout_spin.setDecimals(0)
        self.timeout_spin.setSingleStep(30.0)
        self.timeout_spin.setValue(float(llm_timeout_seconds or 600.0))
        self.timeout_spin.setToolTip("模型请求的总超时。流式响应、首包等待和长输出都受此值影响。")
        network_layout.addRow("模型超时(秒):", self.timeout_spin)

        layout.addWidget(network_group)

        theme_group = QGroupBox("主题")
        theme_layout = QFormLayout(theme_group)
        self.theme_combo = QComboBox()
        configure_combo_popup(self.theme_combo)
        self.theme_combo.addItems(["浅色", "深色"])
        t = (theme or "light").lower()
        self.theme_combo.setCurrentIndex(1 if t == "dark" else 0)
        theme_layout.addRow("界面主题:", self.theme_combo)
        layout.addWidget(theme_group)

        display_group = QGroupBox("显示")
        display_layout = QVBox(display_group)

        self.stats_check = QCheckBox("显示右侧辅助面板")
        self.stats_check.setChecked(bool(show_stats))
        display_layout.addWidget(self.stats_check)

        self.thinking_check = QCheckBox("显示思考过程")
        self.thinking_check.setChecked(bool(show_thinking))
        display_layout.addWidget(self.thinking_check)

        layout.addWidget(display_group)

        debug_group = QGroupBox("诊断")
        debug_layout = QVBox(debug_group)

        self.log_stream_check = QCheckBox("记录流式调试日志")
        self.log_stream_check.setChecked(bool(log_stream))
        debug_layout.addWidget(self.log_stream_check)

        layout.addWidget(debug_group)

        hint = QLabel("代理仅在需要通过本地代理访问模型服务时使用；外观、诊断与基础请求参数集中在这里统一管理。")
        hint.setWordWrap(True)
        hint.setProperty("muted", True)
        layout.addWidget(hint)

        layout.addStretch()

    def collect(self) -> dict:
        return {
            "proxy_url": (self.proxy_edit.text() or "").strip(),
            "llm_timeout_seconds": float(self.timeout_spin.value()),
            "theme": "dark" if self.theme_combo.currentIndex() == 1 else "light",
            "show_stats": bool(self.stats_check.isChecked()),
            "show_thinking": bool(self.thinking_check.isChecked()),
            "log_stream": bool(self.log_stream_check.isChecked()),
        }
