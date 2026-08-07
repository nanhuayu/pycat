"""Composer toolbar extracted from input_area.py."""
from __future__ import annotations

import logging
from PyQt6.QtCore import pyqtSignal, Qt, QSize
from PyQt6.QtGui import QAction, QActionGroup
from PyQt6.QtWidgets import QComboBox, QHBoxLayout, QMenu, QSizePolicy, QToolButton, QWidget

from gui.utils.combo_box import configure_combo_popup
from gui.utils.icon_manager import Icons
from gui.utils.theme import prepare_context_menu, resolve_accent, resolve_theme, theme_tokens
from gui.widgets.model_ref_selector import ModelRefCombo
from gui.widgets.input.context_usage_button import ContextUsageButton


logger = logging.getLogger(__name__)


class ComposerToolbar(QWidget):
    """Compact toolbar with one visible control per conversation setting."""

    _BUTTON_SIZE = QSize(30, 30)
    _PRIMARY_ACTION_BUTTON_SIZE = QSize(30, 30)
    _ICON_SIZE = QSize(18, 18)
    _PRIMARY_ACTION_ICON_SIZE = QSize(20, 20)
    _MODEL_SELECTOR_MIN_WIDTH = 104
    _MODEL_SELECTOR_MAX_WIDTH = 360

    attach_requested = pyqtSignal()
    prompt_optimize_requested = pyqtSignal()
    prompt_optimize_cancel_requested = pyqtSignal()
    primary_action_requested = pyqtSignal()
    model_ref_changed = pyqtSignal(str)
    permission_preset_changed = pyqtSignal(str)
    compact_requested = pyqtSignal()
    session_settings_requested = pyqtSignal()
    model_edit_requested = pyqtSignal()

    _PERMISSION_PRESET_ITEMS = (
        ("默认权限", "default"),
        ("每次确认", "ask"),
        ("禁用工具", "deny"),
        ("全部放行", "allow"),
        ("自定义权限", "custom"),
    )
    _PERMISSION_PRESET_LABELS = {value: label for label, value in _PERMISSION_PRESET_ITEMS}
    _PERMISSION_PRESET_VISUALS = {
        "default": (Icons.SHIELD, "primary"),
        "ask": (Icons.CIRCLE_INFO, "warning"),
        "deny": (Icons.LOCK, "muted"),
        "allow": (Icons.UNLOCK, "danger"),
        "custom": (Icons.SLIDERS, "primary"),
    }
    _PERMISSION_PRESET_DESCRIPTIONS = {
        "default": "按内置安全表执行，部分工具类别需要确认",
        "ask": "每次工具执行前都逐个确认",
        "deny": "不展示或执行普通模型工具",
        "allow": "跳过确认，高风险工具将直接执行",
        "custom": "使用设置页维护的类别与工具规则",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._prompt_optimize_busy = False
        self._suppress_model_ref_signal = False
        self._last_emitted_model_ref = ""
        self._permission_preset = "default"
        self._permission_enabled = False
        self._is_streaming = False
        self._has_draft = False
        self._primary_enabled = True
        self._primary_visual_key: tuple[str, str, str] | None = None
        self._context_busy = False
        self._revision_active = False
        self._show_thinking = True
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.attach_btn = self._make_icon_button(Icons.PAPERCLIP, "添加文件/图片")
        self.attach_btn.clicked.connect(self.attach_requested.emit)
        layout.addWidget(self.attach_btn)

        self.mode_combo = QComboBox()
        self.mode_combo.setObjectName("mode_combo")
        self._configure_combo(self.mode_combo, minimum=64, maximum=88, popup_minimum=180, tooltip="选择对话模式")
        layout.addWidget(self.mode_combo)

        self.permission_btn = self._make_icon_button(Icons.SHIELD, "会话权限预设")
        self.permission_btn.setObjectName("permission_preset_btn")
        self.permission_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._permission_menu = prepare_context_menu(QMenu(self.permission_btn), self)
        self._permission_menu.aboutToShow.connect(
            lambda: prepare_context_menu(self._permission_menu, self)
        )
        self._permission_actions = QActionGroup(self._permission_menu)
        self._permission_actions.setExclusive(True)
        for label, value in self._PERMISSION_PRESET_ITEMS:
            action = QAction(label, self._permission_menu)
            action.setCheckable(True)
            action.setData(value)
            icon_name, tone = self._PERMISSION_PRESET_VISUALS[value]
            action.setIcon(self._permission_icon(icon_name, tone))
            action.setToolTip(self._PERMISSION_PRESET_DESCRIPTIONS[value])
            action.triggered.connect(
                lambda _checked=False, preset=value: self.permission_preset_changed.emit(preset)
            )
            self._permission_actions.addAction(action)
            self._permission_menu.addAction(action)
        self.permission_btn.setMenu(self._permission_menu)
        self._apply_permission_preset_display()
        self._sync_permission_btn_enabled()
        layout.addWidget(self.permission_btn)

        self.context_usage_btn = ContextUsageButton()
        self.context_usage_btn.setProperty("composer_action", True)
        self.context_usage_btn.compact_requested.connect(self.compact_requested.emit)
        layout.addWidget(self.context_usage_btn)

        self.session_options_btn = self._make_icon_button(Icons.SLIDERS, "对话设置")
        self.session_options_btn.setObjectName("session_options_btn")
        self.session_options_btn.clicked.connect(self.session_settings_requested.emit)
        layout.addWidget(self.session_options_btn)
        self._sync_permission_btn_enabled()

        layout.addStretch()

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

        self.model_edit_btn = self._make_icon_button(Icons.EDIT, "编辑模型")
        self.model_edit_btn.setObjectName("model_edit_btn")
        self.model_edit_btn.clicked.connect(self.model_edit_requested.emit)
        layout.addWidget(self.model_edit_btn)
        self._sync_permission_btn_enabled()

        self.prompt_optimize_btn = self._make_icon_button(Icons.WAND, "优化提示词")
        self.prompt_optimize_btn.clicked.connect(self._handle_prompt_optimize_clicked)
        layout.addWidget(self.prompt_optimize_btn)

        self.primary_action_btn = self._make_button("", "发送消息 (Ctrl+Enter)")
        self.primary_action_btn.setObjectName("primary_action_btn")
        self.primary_action_btn.setFixedSize(self._PRIMARY_ACTION_BUTTON_SIZE)
        self.primary_action_btn.clicked.connect(self.primary_action_requested.emit)
        layout.addWidget(self.primary_action_btn)
        self._sync_primary_action()

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
        self._last_emitted_model_ref = self.model_ref_combo.model_ref()
        self._compact_model_ref_combo_width()
        self._sync_permission_btn_enabled()

    def set_model_ref(self, model_ref: str) -> None:
        self._suppress_model_ref_signal = True
        self.model_ref_combo.blockSignals(True)
        try:
            self.model_ref_combo.set_model_ref(model_ref or "")
        finally:
            self.model_ref_combo.blockSignals(False)
            self._suppress_model_ref_signal = False
        self._last_emitted_model_ref = self.model_ref_combo.model_ref()
        self._compact_model_ref_combo_width()
        self._sync_permission_btn_enabled()

    def model_ref(self) -> str:
        return self.model_ref_combo.model_ref()

    def set_permission_preset(self, preset: str) -> None:
        normalized = str(preset or "").strip() or "default"
        if normalized not in self._PERMISSION_PRESET_LABELS:
            normalized = "default"
        self._permission_preset = normalized
        self._apply_permission_preset_display()

    def permission_preset(self) -> str:
        return self._permission_preset

    def set_permission_enabled(self, enabled: bool) -> None:
        self._permission_enabled = bool(enabled)
        self._sync_permission_btn_enabled()

    def _apply_permission_preset_display(self) -> None:
        label = self._PERMISSION_PRESET_LABELS.get(
            self._permission_preset, self._PERMISSION_PRESET_LABELS["default"]
        )
        icon_name, tone = self._PERMISSION_PRESET_VISUALS[self._permission_preset]
        description = self._PERMISSION_PRESET_DESCRIPTIONS.get(self._permission_preset, "")
        # Icon-only control: the preset name and its behavior live in the
        # tooltip/accessible name and in the menu items, not on the button face.
        self.permission_btn.setIcon(self._permission_icon(icon_name, tone))
        self.permission_btn.setProperty("tone", tone)
        tooltip = f"会话权限预设：{label}"
        if description:
            tooltip = f"{tooltip}\n{description}"
        self.permission_btn.setToolTip(tooltip)
        self.permission_btn.setAccessibleName(tooltip.replace("\n", "，"))
        for action in self._permission_actions.actions():
            action.setChecked(action.data() == self._permission_preset)
        self.permission_btn.style().unpolish(self.permission_btn)
        self.permission_btn.style().polish(self.permission_btn)

    @staticmethod
    def _permission_icon(icon_name: str, tone: str):
        icon_factory = {
            "warning": Icons.get_warning,
            "danger": Icons.get_error,
            "muted": Icons.get_muted,
        }.get(tone, Icons.get)
        return icon_factory(icon_name, scale_factor=0.9)

    def _sync_permission_btn_enabled(self) -> None:
        self.permission_btn.setEnabled(
            self._permission_enabled and not self._context_busy
        )
        session_options_btn = getattr(self, "session_options_btn", None)
        if session_options_btn is not None:
            session_options_btn.setEnabled(
                self._permission_enabled
                and not self._is_streaming
                and not self._context_busy
            )
        model_edit_btn = getattr(self, "model_edit_btn", None)
        if model_edit_btn is not None:
            model_edit_btn.setEnabled(
                bool(self.model_ref())
                and not self._is_streaming
                and not self._context_busy
            )

    def set_show_thinking(self, enabled: bool) -> None:
        self._show_thinking = bool(enabled)

    def is_show_thinking_enabled(self) -> bool:
        return bool(self._show_thinking)

    def set_token_snapshot(self, snapshot) -> None:
        self.context_usage_btn.set_snapshot(snapshot)

    def _emit_model_ref_changed(self) -> None:
        if self._suppress_model_ref_signal:
            return
        model_ref = self.model_ref_combo.model_ref()
        if model_ref == self._last_emitted_model_ref:
            return
        self._last_emitted_model_ref = model_ref
        self.model_ref_changed.emit(model_ref)

    def _compact_model_ref_combo_width(self, *_args) -> None:
        try:
            model_ref = self.model_ref_combo.model_ref()
            text = self.model_ref_combo.currentText() or model_ref or "选择模型"
            line_edit = self.model_ref_combo.lineEdit()
            current_index = self.model_ref_combo.currentIndex()
            selected_text = (
                self.model_ref_combo.itemText(current_index).strip()
                if current_index >= 0
                else ""
            )
            if model_ref and line_edit.text().strip() == selected_text:
                line_edit.setCursorPosition(0)
            self.model_ref_combo.setToolTip(
                f"选择当前对话模型\n当前：{model_ref}" if model_ref else "选择当前对话模型"
            )
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
        button.setProperty("composer_action", True)
        button.setFixedSize(self._BUTTON_SIZE)
        button.setIconSize(self._ICON_SIZE)
        button.setToolTip(tooltip)
        return button

    def set_streaming_state(self, is_streaming: bool) -> None:
        self._is_streaming = bool(is_streaming)
        self._sync_primary_action()
        self.attach_btn.setEnabled(not self._is_streaming and not self._context_busy)
        self.mode_combo.setEnabled(
            not self._is_streaming and not self._context_busy
        )
        self.model_ref_combo.setEnabled(
            not self._is_streaming and not self._context_busy
        )
        self._sync_permission_btn_enabled()
        self.context_usage_btn.set_streaming_state(self._is_streaming)
        if is_streaming:
            self.prompt_optimize_btn.setEnabled(False)
            return

        self.prompt_optimize_btn.setEnabled(True)

    def set_context_busy(self, busy: bool) -> None:
        self._context_busy = bool(busy)
        self.context_usage_btn.set_busy_state(self._context_busy)
        self.mode_combo.setEnabled(
            not self._is_streaming and not self._context_busy
        )
        self.model_ref_combo.setEnabled(
            not self._is_streaming and not self._context_busy
        )
        self.prompt_optimize_btn.setEnabled(not self._is_streaming and not self._context_busy)
        self._sync_permission_btn_enabled()
        self._sync_primary_action()

    def set_primary_action_state(self, *, has_draft: bool, enabled: bool) -> None:
        normalized_draft = bool(has_draft)
        normalized_enabled = bool(enabled)
        if (
            normalized_draft == self._has_draft
            and normalized_enabled == self._primary_enabled
        ):
            return
        self._has_draft = normalized_draft
        self._primary_enabled = normalized_enabled
        self.primary_action_btn.setEnabled(normalized_enabled)
        self._sync_primary_action()

    def set_revision_state(self, active: bool) -> None:
        normalized = bool(active)
        if normalized == self._revision_active:
            return
        self._revision_active = normalized
        self._primary_visual_key = None
        self._sync_primary_action()

    def _sync_primary_action(self) -> None:
        stopping = self._is_streaming and not self._has_draft
        action = "stop" if stopping else (
            "guide" if self._is_streaming else ("revision" if self._revision_active else "send")
        )
        theme = resolve_theme(self)
        accent = resolve_accent(self)
        visual_key = (action, theme, accent)
        if visual_key == self._primary_visual_key:
            return
        tokens = theme_tokens(theme, accent)
        try:
            if stopping:
                icon = Icons.get(
                    Icons.STOP_FILLED,
                    color=tokens.color("error"),
                    scale_factor=1.0,
                )
            elif self._revision_active and not self._is_streaming:
                icon = Icons.get(
                    Icons.REFRESH,
                    color=tokens.color("on_primary"),
                    scale_factor=1.0,
                )
            else:
                icon = Icons.get(
                    Icons.SEND,
                    color=tokens.color("on_primary"),
                    scale_factor=1.0,
                )
            self.primary_action_btn.setIcon(icon)
            self.primary_action_btn.setIconSize(self._PRIMARY_ACTION_ICON_SIZE)
        except Exception as exc:
            logger.debug("Failed to set primary action icon on composer toolbar: %s", exc)
        self.primary_action_btn.setText("")
        if stopping:
            tooltip = "停止当前任务 (Esc)"
            tone = "danger"
        elif self._is_streaming:
            tooltip = "引导当前任务 (Ctrl+Enter；Esc 停止)"
            tone = "primary"
        elif self._revision_active:
            tooltip = "替换并重试 (Ctrl+Enter)"
            tone = "primary"
        else:
            tooltip = "发送消息 (Ctrl+Enter)"
            tone = "primary"
        self.primary_action_btn.setToolTip(tooltip)
        self.primary_action_btn.setAccessibleName(tooltip)
        self.primary_action_btn.setProperty("tone", tone)
        self.primary_action_btn.style().unpolish(self.primary_action_btn)
        self.primary_action_btn.style().polish(self.primary_action_btn)
        self._primary_visual_key = visual_key

    def refresh_theme(self) -> None:
        self._primary_visual_key = None
        self._sync_primary_action()

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
