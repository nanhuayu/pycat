"""Composer toolbar extracted from input_area.py."""
from __future__ import annotations

import logging
from PyQt6.QtCore import pyqtSignal, Qt, QSize
from PyQt6.QtWidgets import (
    QComboBox, QHBoxLayout, QSizePolicy, QToolButton, QWidget,
)

from ui.utils.combo_box import configure_combo_popup
from ui.utils.icon_manager import Icons


logger = logging.getLogger(__name__)


class ComposerToolbar(QWidget):
    """Toolbar widget for provider/model/mode controls."""

    _BUTTON_SIZE = QSize(28, 28)
    _ICON_SIZE = QSize(20, 20)

    attach_requested = pyqtSignal()
    conversation_settings_requested = pyqtSignal()
    provider_settings_requested = pyqtSignal()
    prompt_optimize_requested = pyqtSignal()
    prompt_optimize_cancel_requested = pyqtSignal()
    send_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._prompt_optimize_busy = False
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.attach_btn = self._make_icon_button(Icons.PAPERCLIP, "添加文件/图片")
        self.attach_btn.clicked.connect(self.attach_requested.emit)
        layout.addWidget(self.attach_btn)

        self.provider_combo = QComboBox()
        self.provider_combo.setObjectName("provider_combo")
        self._configure_combo(self.provider_combo, minimum=86, maximum=146, popup_minimum=220, tooltip="选择服务商")
        layout.addWidget(self.provider_combo)
        self.provider_combo.setVisible(False)

        self.model_combo = QComboBox()
        self.model_combo.setObjectName("model_combo")
        self.model_combo.setEditable(True)
        self._configure_combo(self.model_combo, minimum=168, maximum=280, popup_minimum=320, tooltip="选择模型")
        layout.addWidget(self.model_combo)
        self.model_combo.setVisible(False)

        self.mode_combo = QComboBox()
        self.mode_combo.setObjectName("mode_combo")
        self._configure_combo(self.mode_combo, minimum=90, maximum=150, popup_minimum=220, tooltip="选择对话模式")
        layout.addWidget(self.mode_combo)

        self.thinking_toggle = self._make_icon_toggle(Icons.BRAIN, "显示思考过程")
        layout.addWidget(self.thinking_toggle)

        self.mcp_toggle = self._make_icon_toggle(Icons.PLUG, "启用 MCP 工具")
        layout.addWidget(self.mcp_toggle)

        self.search_toggle = self._make_icon_toggle(Icons.SEARCH, "启用网络搜索")
        layout.addWidget(self.search_toggle)

        self.conv_settings_btn = self._make_icon_button(Icons.SETTINGS, "对话设置 (采样参数/系统提示)")
        self.conv_settings_btn.clicked.connect(self.conversation_settings_requested.emit)
        layout.addWidget(self.conv_settings_btn)

        self.provider_settings_btn = self._make_icon_button(Icons.WRENCH, "配置服务商 (API/Key/模型列表)")
        self.provider_settings_btn.clicked.connect(self.provider_settings_requested.emit)
        layout.addWidget(self.provider_settings_btn)

        layout.addStretch()

        self.prompt_optimize_btn = self._make_icon_button(Icons.WAND, "优化提示词")
        self.prompt_optimize_btn.clicked.connect(self._handle_prompt_optimize_clicked)
        layout.addWidget(self.prompt_optimize_btn)

        self.send_btn = self._make_button("", "发送消息 (Ctrl+Enter)")
        self._set_send_button_icon(is_streaming=False, style=self.style())
        self.send_btn.clicked.connect(self.send_requested.emit)
        layout.addWidget(self.send_btn)

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def _make_button(self, text: str, tooltip: str) -> QToolButton:
        button = QToolButton()
        button.setObjectName("toolbar_btn")
        button.setText(text)
        button.setToolTip(tooltip)
        button.setFixedSize(self._BUTTON_SIZE)
        button.setIconSize(self._ICON_SIZE)
        return button

    def _make_icon_button(self, icon_name: str, tooltip: str) -> QToolButton:
        button = QToolButton()
        button.setObjectName("toolbar_btn")
        button.setIcon(Icons.get(icon_name, scale_factor=1.0))
        button.setFixedSize(self._BUTTON_SIZE)
        button.setIconSize(self._ICON_SIZE)
        button.setToolTip(tooltip)
        return button

    def _make_icon_toggle(self, icon_name: str, tooltip: str) -> QToolButton:
        button = self._make_icon_button(icon_name, tooltip)
        button.setCheckable(True)
        return button

    def set_streaming_state(self, is_streaming: bool, style) -> None:
        self._set_send_button_icon(is_streaming=is_streaming, style=style)
        if is_streaming:
            self.prompt_optimize_btn.setEnabled(False)
            self.mcp_toggle.setEnabled(False)
            self.search_toggle.setEnabled(False)
            return

        self.prompt_optimize_btn.setEnabled(True)

    def _set_send_button_icon(self, *, is_streaming: bool, style) -> None:
        try:
            icon = (
                Icons.get(Icons.STOP, color=Icons.COLOR_ERROR, scale_factor=1.0)
                if is_streaming
                else Icons.get(Icons.SEND, scale_factor=1.0)
            )
            self.send_btn.setIcon(icon)
            self.send_btn.setIconSize(self._ICON_SIZE)
        except Exception as exc:
            logger.debug("Failed to set send button icon on composer toolbar: %s", exc)
        self.send_btn.setText("")
        self.send_btn.setToolTip("停止生成" if is_streaming else "发送消息 (Ctrl+Enter)")

    def set_prompt_optimize_busy(self, busy: bool, *, is_streaming: bool) -> None:
        self._prompt_optimize_busy = bool(busy)
        self.prompt_optimize_btn.setEnabled(not is_streaming)
        if busy:
            self.prompt_optimize_btn.setIcon(Icons.get(Icons.STOP, color=Icons.COLOR_ERROR, scale_factor=1.0))
            self.prompt_optimize_btn.setToolTip("取消提示词优化")
        else:
            self.prompt_optimize_btn.setIcon(Icons.get(Icons.WAND, scale_factor=1.0))
            self.prompt_optimize_btn.setToolTip("优化提示词")

    def _handle_prompt_optimize_clicked(self) -> None:
        if self._prompt_optimize_busy:
            self.prompt_optimize_cancel_requested.emit()
            return
        self.prompt_optimize_requested.emit()

    def _configure_combo(
        self,
        combo: QComboBox,
        *,
        minimum: int,
        maximum: int,
        popup_minimum: int,
        tooltip: str,
    ) -> None:
        combo.setMinimumWidth(minimum)
        combo.setMaximumWidth(maximum)
        combo.setMaxVisibleItems(18)
        combo.setToolTip(tooltip)
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContentsOnFirstShow)
        configure_combo_popup(combo, popup_minimum_width=popup_minimum)
