"""Dependency-light controls shared by settings forms."""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QSizePolicy,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from pycat.gui.utils.theme import COMPACT_CONTROL_HEIGHT

# Settings use a small, stable geometry vocabulary.  Individual pages may
# widen a resource selector, but should not invent another baseline height or
# field width.
SETTINGS_FORM_MAX_WIDTH = 720
SETTINGS_SINGLE_LINE_HEIGHT = COMPACT_CONTROL_HEIGHT
SETTINGS_FIELD_WIDTH = 200


_SETTINGS_CONTROL_TYPES = (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QTextEdit,
)

_SETTINGS_SINGLE_LINE_TYPES = (QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox)


def mark_settings_controls(widget: QWidget) -> None:
    """Mark a form field and nested editors for settings-only styling."""

    controls = (widget, *widget.findChildren(QWidget))
    for control in controls:
        if not isinstance(control, _SETTINGS_CONTROL_TYPES):
            continue
        if isinstance(control, QLineEdit) and isinstance(control.parentWidget(), QAbstractSpinBox):
            continue  # The spin box owns its internal editor's size and appearance.
        control.setProperty("settings_control", True)
        if isinstance(control, _SETTINGS_SINGLE_LINE_TYPES):
            control.setMinimumHeight(SETTINGS_SINGLE_LINE_HEIGHT)
            control.setMinimumWidth(min(control.maximumWidth(), max(control.minimumWidth(), SETTINGS_FIELD_WIDTH)))


class SettingsToggle(QWidget):
    """A settings boolean with its label at left and indicator at right."""

    toggled = pyqtSignal(bool)

    def __init__(self, label: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("settings_toggle")
        self.setProperty("settings_toggle", True)
        self.setMinimumHeight(SETTINGS_SINGLE_LINE_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        self.label = QLabel(str(label or ""))
        self.label.setObjectName("settings_toggle_label")
        self.label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.label, 1)

        self.control = QCheckBox()
        self.control.setObjectName("settings_toggle_control")
        self.control.setAccessibleName(str(label or ""))
        self.control.setFixedSize(20, 20)
        self.control.toggled.connect(self.toggled.emit)
        layout.addWidget(self.control, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def text(self) -> str:
        return self.label.text()

    def isChecked(self) -> bool:
        return self.control.isChecked()

    def setChecked(self, checked: bool) -> None:
        self.control.setChecked(bool(checked))

    def setEnabled(self, enabled: bool) -> None:
        super().setEnabled(bool(enabled))
        self.control.setEnabled(bool(enabled))
        self.label.setEnabled(bool(enabled))

    def setToolTip(self, text: str) -> None:
        super().setToolTip(text)
        self.control.setToolTip(text)


class _RowCard(QFrame):
    """A settings row that collapses when all of its content hides.

    Actionable and informational rows have separate style hooks. Stacked
    rows leave the label above the field without a row divider. Conditional
    visibility follows the caller-owned title and field widgets.
    """

    def __init__(self, info: bool = False) -> None:
        super().__init__()
        self.setObjectName("settings_info_row" if info else "settings_row_card")

    def track(self, widget: QWidget) -> None:
        widget.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in (
            QEvent.Type.Show,
            QEvent.Type.Hide,
            QEvent.Type.ShowToParent,
            QEvent.Type.HideToParent,
        ):
            # Defer: touching widget visibility while a Show/Hide event is
            # still being delivered would reenter Qt's internal show state.
            QTimer.singleShot(0, self._sync_visibility)
        return super().eventFilter(watched, event)

    def _sync_visibility(self) -> None:
        try:
            # NOTE: the name argument must stay omitted — an explicit "" filters
            # by objectName and would drop named children such as SettingsToggle
            # ("settings_toggle"), collapsing toggle rows right after page show.
            children = self.findChildren(
                QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly
            )
            self.setVisible(any(child.isVisibleTo(self) for child in children))
        except RuntimeError:
            # The card's C++ object may already be gone during teardown.
            pass


class SettingsFormLayout(QFormLayout):
    """Apply common spacing and label placement to settings fields.

    Short controls use compact inline rows. Multiline editors, checkbox
    groups and pages opting into ``stacked_labels`` place their label above
    a full-width field. ``info=True`` marks non-editable explanatory rows.
    """

    _RIGHT_ALIGNED_TYPES = (QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox, QLabel)
    _STRETCH_TYPES = (QTextEdit, QPlainTextEdit, SettingsToggle)

    def __init__(self, parent=None, *, stacked_labels=False) -> None:
        super().__init__(parent)
        self._stacked_labels = stacked_labels
        self.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.setVerticalSpacing(6)

    def addRow(self, *args, info: bool = False) -> None:  # noqa: N802 - Qt API spelling
        """Add a labeled field or a full-width widget using the page's layout."""

        label = args[0] if len(args) == 2 and isinstance(args[0], QWidget) else None
        label_text = "" if label is not None else (
            str(args[0]) if len(args) == 2 and isinstance(args[1], QWidget) else ""
        )
        field = args[1] if len(args) == 2 and isinstance(args[1], QWidget) else args[0]
        if not isinstance(field, QWidget):
            super().addRow(*args)
            return
        mark_settings_controls(field)

        card = _RowCard(info=info)
        stacked = (self._stacked_labels or isinstance(field, (QTextEdit, QPlainTextEdit)) or bool(field.findChildren(QCheckBox))) and (label is not None or bool(label_text))
        row = QVBoxLayout(card) if stacked else QHBoxLayout(card)
        card.setProperty("stacked", bool(stacked))
        row.setContentsMargins(0, 6, 0, 8) if stacked else row.setContentsMargins(10, 5, 10, 5)
        row.setSpacing(8)
        if label is not None:
            # Reuse the caller's widget so external ``setVisible``/``setEnabled``
            # references keep controlling the row title.
            card.track(label)
            row.addWidget(label, 0, Qt.AlignmentFlag.AlignLeft if stacked else Qt.AlignmentFlag.AlignVCenter)
        elif label_text:
            title = QLabel(label_text)
            title.setWordWrap(True)
            row.addWidget(title, 0, Qt.AlignmentFlag.AlignLeft if stacked else Qt.AlignmentFlag.AlignVCenter)
        card.track(field)
        if stacked:
            field.setSizePolicy(QSizePolicy.Policy.Expanding, field.sizePolicy().verticalPolicy())
            row.addWidget(field)
        elif (info and len(args) == 1) or isinstance(field, self._STRETCH_TYPES) or not isinstance(
            field, self._RIGHT_ALIGNED_TYPES
        ):
            row.addWidget(field, 1)
        else:
            row.addStretch(1)
            row.addWidget(field, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setWidget(self.rowCount(), QFormLayout.ItemRole.SpanningRole, card)
