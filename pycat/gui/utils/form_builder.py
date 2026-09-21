"""Reusable form-building helpers for settings pages and dialogs.

Eliminates repeated settings-section and widget-configuration
boilerplate that appears across 10+ settings pages and 3+ dialogs.

Usage::

    sec = FormSection("采样参数")
    self.temperature = sec.add_double_spin("温度", value=0.7, range=(0, 2))
    self.top_p = sec.add_double_spin("Top P", value=1.0)
    layout.addWidget(sec.group)
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
)

from pycat.gui.utils.combo_box import configure_combo_popup
from pycat.gui.utils.settings_controls import (
    SETTINGS_FORM_MAX_WIDTH,
    SettingsFormLayout,
    SettingsToggle,
)
from pycat.gui.widgets.themed_line_edit import ThemedLineEdit, ThemedTextEdit


class FormSection:
    """A styled section surface wrapping a ``QFormLayout``."""

    def __init__(self, title: str) -> None:
        self.group = QFrame()
        self.group.setObjectName("settings_form_section")
        self.group.setMaximumWidth(SETTINGS_FORM_MAX_WIDTH)
        self.group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        root = QVBoxLayout(self.group)
        root.setContentsMargins(0, 6, 0, 6)
        root.setSpacing(8)
        heading = QLabel(str(title or ""))
        heading.setObjectName("settings_section_title")
        root.addWidget(heading)
        form_host = QFrame()
        form_host.setObjectName("settings_section_body")
        self.form = SettingsFormLayout(form_host)
        self.form.setHorizontalSpacing(10)
        self.form.setVerticalSpacing(6)
        root.addWidget(form_host)

    # ------------------------------------------------------------------
    # Field helpers — each returns the created widget so callers can
    # assign it to ``self.<name>`` for later retrieval in ``collect()``.
    # ------------------------------------------------------------------

    def add_line_edit(
        self,
        label: str | QLabel,
        *,
        text: str = "",
        placeholder: str = "",
        echo_password: bool = False,
        object_name: str | None = None,
    ) -> QLineEdit:
        w = ThemedLineEdit()
        if text:
            w.setText(text)
        if placeholder:
            w.setPlaceholderText(placeholder)
        if echo_password:
            w.setEchoMode(QLineEdit.EchoMode.Password)
        if object_name:
            w.setObjectName(object_name)
        self.form.addRow(label, w)
        return w

    def add_text_edit(
        self,
        label: str,
        *,
        text: str = "",
        placeholder: str = "",
        max_height: int = 80,
        object_name: str | None = None,
    ) -> QTextEdit:
        w = ThemedTextEdit()
        w.setAcceptRichText(False)
        w.setMaximumHeight(max_height)
        if text:
            w.setText(text)
        if placeholder:
            w.setPlaceholderText(placeholder)
        if object_name:
            w.setObjectName(object_name)
        self.form.addRow(label, w)
        return w

    def add_spin(
        self,
        label: str,
        *,
        value: int = 0,
        range: tuple[int, int] = (0, 100),
        step: int = 1,
        tooltip: str = "",
        object_name: str | None = None,
    ) -> QSpinBox:
        w = QSpinBox()
        w.setRange(*range)
        w.setSingleStep(step)
        w.setValue(value)
        if tooltip:
            w.setToolTip(tooltip)
        if object_name:
            w.setObjectName(object_name)
        self.form.addRow(label, w)
        return w

    def add_double_spin(
        self,
        label: str,
        *,
        value: float = 0.0,
        range: tuple[float, float] = (0.0, 1.0),
        step: float = 0.05,
        decimals: int = 2,
        tooltip: str = "",
        object_name: str | None = None,
    ) -> QDoubleSpinBox:
        w = QDoubleSpinBox()
        w.setRange(*range)
        w.setSingleStep(step)
        w.setDecimals(decimals)
        w.setValue(value)
        if tooltip:
            w.setToolTip(tooltip)
        if object_name:
            w.setObjectName(object_name)
        self.form.addRow(label, w)
        return w

    def add_combo(
        self,
        label: str,
        *,
        items: list[str] | None = None,
        editable: bool = False,
        current_text: str = "",
        current_index: int = -1,
        object_name: str | None = None,
    ) -> QComboBox:
        w = QComboBox()
        if items:
            w.addItems(items)
        w.setEditable(editable)
        w.setMaxVisibleItems(18)
        try:
            w.view().setTextElideMode(Qt.TextElideMode.ElideMiddle)
        except Exception:
            pass
        if current_index >= 0:
            w.setCurrentIndex(current_index)
        elif current_text:
            w.setCurrentText(current_text)
        if object_name:
            w.setObjectName(object_name)
        configure_combo_popup(w)
        self.form.addRow(label, w)
        return w

    def add_checkbox(
        self,
        label: str,
        *,
        checked: bool = False,
        tooltip: str = "",
        row_label: str = "",
        object_name: str | None = None,
    ) -> "SettingsToggle":
        w = SettingsToggle(label)
        w.setChecked(checked)
        if tooltip:
            w.setToolTip(tooltip)
        if object_name:
            w.setObjectName(object_name)
        if row_label:
            self.form.addRow(row_label, w)
        else:
            # A standalone boolean is a complete row: keep its title at the
            # left edge and the indicator at the far right.
            self.form.addRow(w)
        return w
