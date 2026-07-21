"""Composer toolbar extracted from input_area.py."""
from __future__ import annotations

import logging
from PyQt6.QtCore import pyqtSignal, Qt, QSize
from PyQt6.QtWidgets import (
    QComboBox, QHBoxLayout, QSizePolicy, QToolButton, QWidget,
)

from gui.utils.combo_box import configure_combo_popup
from gui.utils.icon_manager import Icons
from gui.utils.theme import theme_tokens
from gui.widgets.model_ref_selector import ModelRefCombo


logger = logging.getLogger(__name__)


class ComposerToolbar(QWidget):
    """Toolbar widget for provider/model/mode controls."""

    _BUTTON_SIZE = QSize(30, 30)
    _SEND_BUTTON_SIZE = QSize(30, 30)
    _ICON_SIZE = QSize(18, 18)
    _SEND_ICON_SIZE = QSize(20, 20)
    _MODEL_SELECTOR_MIN_WIDTH = 104
    _MODEL_SELECTOR_MAX_WIDTH = 164

    attach_requested = pyqtSignal()
    conversation_settings_requested = pyqtSignal()
    model_edit_requested = pyqtSignal()
    prompt_optimize_requested = pyqtSignal()
    prompt_optimize_cancel_requested = pyqtSignal()
    send_requested = pyqtSignal()
    model_ref_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._prompt_optimize_busy = False
        self._suppress_model_ref_signal = False
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
        self._configure_combo(self.mode_combo, minimum=72, maximum=98, popup_minimum=180, tooltip="选择对话模式")
        layout.addWidget(self.mode_combo)

        self.thinking_toggle = self._make_icon_toggle(Icons.THINKING, "显示思考过程")
        self.thinking_toggle.setToolTip("显示思考过程（可在设置中调整，默认弱化显示）")
        self.thinking_toggle.setVisible(False)
        layout.addWidget(self.thinking_toggle)

        self.conv_settings_btn = self._make_icon_button(Icons.SETTINGS, "对话设置 (采样参数/系统提示)")
        self.conv_settings_btn.clicked.connect(self.conversation_settings_requested.emit)
        layout.addWidget(self.conv_settings_btn)

        layout.addStretch()

        self.edit_model_btn = self._make_icon_button(Icons.EDIT, "编辑当前模型")
        self.edit_model_btn.clicked.connect(self.model_edit_requested.emit)
        layout.addWidget(self.edit_model_btn)

        self.model_ref_combo = ModelRefCombo(
            [],
            allow_empty=False,
            empty_label="选择模型",
            allow_unlisted_current=True,
        )
        self.model_ref_combo.setObjectName("bottom_model_selector")
        self.model_ref_combo.setMinimumContentsLength(1)
        self.model_ref_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.model_ref_combo.setMinimumWidth(self._MODEL_SELECTOR_MIN_WIDTH)
        self.model_ref_combo.setMaximumWidth(self._MODEL_SELECTOR_MAX_WIDTH)
        self.model_ref_combo.setFixedHeight(self._BUTTON_SIZE.height())
        self.model_ref_combo.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.model_ref_combo.setToolTip("选择当前对话模型")
        try:
            line_edit = self.model_ref_combo.lineEdit()
            line_edit.setTextMargins(0, 0, 0, 0)
            line_edit.setFrame(False)
            line_edit.setClearButtonEnabled(False)
            line_edit.setMinimumWidth(0)
        except Exception as exc:
            logger.debug("Failed to compact bottom model selector line edit: %s", exc)
        self.model_ref_combo.currentIndexChanged.connect(self._emit_model_ref_changed)
        self.model_ref_combo.currentTextChanged.connect(self._compact_model_ref_combo_width)
        try:
            self.model_ref_combo.lineEdit().editingFinished.connect(self._emit_model_ref_changed)
        except Exception as exc:
            logger.debug("Failed to connect bottom model selector editingFinished: %s", exc)
        self._compact_model_ref_combo_width()
        layout.addWidget(self.model_ref_combo)

        self.prompt_optimize_btn = self._make_icon_button(Icons.WAND, "优化提示词")
        self.prompt_optimize_btn.clicked.connect(self._handle_prompt_optimize_clicked)
        layout.addWidget(self.prompt_optimize_btn)

        self.send_btn = self._make_button("", "发送消息 (Ctrl+Enter)")
        self.send_btn.setObjectName("send_btn")
        self.send_btn.setFixedSize(self._SEND_BUTTON_SIZE)
        self._set_send_button_icon(is_streaming=False, style=self.style())
        self.send_btn.clicked.connect(self.send_requested.emit)
        layout.addWidget(self.send_btn)

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_model_ref_options(self, providers: list, current_model_ref: str = "") -> None:
        current = current_model_ref or self.model_ref_combo.model_ref()
        self._suppress_model_ref_signal = True
        self.model_ref_combo.blockSignals(True)
        try:
            self.model_ref_combo.set_providers(providers or [], current_model_ref=current)
        finally:
            self.model_ref_combo.blockSignals(False)
            self._suppress_model_ref_signal = False
        self._compact_model_ref_combo_width()

    def set_model_ref(self, model_ref: str) -> None:
        self._suppress_model_ref_signal = True
        self.model_ref_combo.blockSignals(True)
        try:
            self.model_ref_combo.set_model_ref(model_ref or "")
        finally:
            self.model_ref_combo.blockSignals(False)
            self._suppress_model_ref_signal = False
        self._compact_model_ref_combo_width()

    def model_ref(self) -> str:
        return self.model_ref_combo.model_ref()

    def _emit_model_ref_changed(self) -> None:
        if self._suppress_model_ref_signal:
            return
        self.model_ref_changed.emit(self.model_ref_combo.model_ref())

    def _compact_model_ref_combo_width(self, *_args) -> None:
        try:
            text = self.model_ref_combo.currentText() or self.model_ref_combo.model_ref() or "选择模型"
            text_width = self.model_ref_combo.fontMetrics().horizontalAdvance(str(text))
            width = text_width + 22
            width = max(self._MODEL_SELECTOR_MIN_WIDTH, min(self._MODEL_SELECTOR_MAX_WIDTH, width))
            self.model_ref_combo.setFixedWidth(width)
        except Exception as exc:
            logger.debug("Failed to compact bottom model selector width: %s", exc)

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
        self.send_btn.setEnabled(True)
        if is_streaming:
            self.prompt_optimize_btn.setEnabled(False)
            return

        self.prompt_optimize_btn.setEnabled(True)

    def _set_send_button_icon(self, *, is_streaming: bool, style) -> None:
        try:
            on_primary = theme_tokens("light").color("on_primary")
            icon = (
                Icons.get(Icons.STOP_FILLED, color=on_primary, scale_factor=1.1)
                if is_streaming
                else Icons.get(Icons.SEND, color=on_primary, scale_factor=1.0)
            )
            self.send_btn.setIcon(icon)
            self.send_btn.setIconSize(self._SEND_ICON_SIZE)
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
        combo.setFixedHeight(self._BUTTON_SIZE.height())
        combo.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        combo.setMaxVisibleItems(18)
        combo.setToolTip(tooltip)
        combo.setMinimumContentsLength(max(4, min(12, minimum // 8)))
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContentsOnFirstShow)
        configure_combo_popup(combo, popup_minimum_width=popup_minimum)
