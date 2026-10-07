"""Dependency-light controls shared by settings forms."""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractSpinBox,
    QBoxLayout,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTextEdit,
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
        if isinstance(control, QComboBox) and control.sizeAdjustPolicy() == QComboBox.SizeAdjustPolicy.AdjustToContentsOnFirstShow:
            # Long model identifiers belong in the popup, not the page's
            # minimum width. Leave a caller's non-default sizing policy alone.
            control.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)


class SettingsToggle(QWidget):
    """A settings boolean with its label at left and indicator at right."""

    toggled = pyqtSignal(bool)

    def __init__(self, label: str, parent=None) -> None:
        """Keep a native checkbox while making its labeled row one click target."""
        super().__init__(parent)
        self.setObjectName("settings_toggle")
        self.setProperty("settings_toggle", True)
        self.setProperty("focused", False)
        self.setMinimumHeight(SETTINGS_SINGLE_LINE_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._pressed = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        self.label = QLabel(str(label or ""))
        self.label.setObjectName("settings_toggle_label")
        self.label.setWordWrap(True)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.label, 1)

        self.control = QCheckBox()
        self.control.setObjectName("settings_toggle_control")
        self.control.setAccessibleName(str(label or ""))
        self.control.setFixedSize(20, 20)
        self.control.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.control.installEventFilter(self)
        self.control.toggled.connect(self.toggled.emit)
        layout.addWidget(self.control, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.label.setBuddy(self.control)
        self.setFocusProxy(self.control)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def text(self) -> str:
        """Return the visible label, preserving the settings checkbox API."""
        return self.label.text()

    def isChecked(self) -> bool:
        """Read the native checkbox's current value."""
        return self.control.isChecked()

    def setChecked(self, checked: bool) -> None:
        """Set the native value and retain its standard toggled signal behavior."""
        self.control.setChecked(bool(checked))

    def setEnabled(self, enabled: bool) -> None:
        """Disable the complete hit target and clear any pending row press."""
        self._pressed = False
        super().setEnabled(bool(enabled))
        self.control.setEnabled(bool(enabled))
        self.label.setEnabled(bool(enabled))

    def setToolTip(self, text: str) -> None:
        """Expose the same help over both the row and its checkbox."""
        super().setToolTip(text)
        self.control.setToolTip(text)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Project native keyboard focus onto the row's stylesheet hook."""
        if watched is self.control and event.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            self.setProperty("focused", event.type() == QEvent.Type.FocusIn)
            self.style().unpolish(self)
            self.style().polish(self)
            self.update()
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event) -> None:
        """Arm a left-button row click without replacing checkbox keyboard handling."""
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self._pressed = True
            self.control.setFocus(Qt.FocusReason.MouseFocusReason)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        """Commit a completed row click once; indicator clicks remain native."""
        pressed = self._pressed
        self._pressed = False
        if event.button() == Qt.MouseButton.LeftButton and pressed:
            if self.isEnabled() and self.rect().contains(event.position().toPoint()):
                self.control.click()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class _RowCard(QFrame):
    """A settings row that collapses when all of its content hides.

    Actionable and informational rows have separate style hooks. Stacked
    rows leave the label above the field without a row divider. Conditional
    visibility follows the caller-owned title and field widgets.
    """

    def __init__(self, field: QWidget, label: QWidget | None, *, info: bool, stacked: bool, stretch: bool) -> None:
        """Own one existing row and reflow its widgets without adding wrappers."""
        super().__init__()
        self.setObjectName("settings_info_row" if info else "settings_row_card")
        self._field = field
        self._label = label
        self._force_stacked = stacked
        self._stretch = stretch
        self._row = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        if label is not None:
            self._row.addWidget(label)
            self.track(label)
        self._row.addWidget(field)
        self.track(field)
        self._set_stacked(stacked)

    def minimumSizeHint(self) -> QSize:
        """Allow a labeled inline row to shrink far enough to become stacked."""
        hint = super().minimumSizeHint()
        if self._label is not None:
            hint.setWidth(max(self._field.minimumWidth(), self._field.minimumSizeHint().width()))
        return hint

    def resizeEvent(self, event) -> None:
        """Select row direction using the available width and readable label space."""
        super().resizeEvent(event)
        if self._label is not None:
            label_width = min(280, max(120, self._label.sizeHint().width()))
            field_width = max(self._field.minimumWidth(), self._field.minimumSizeHint().width())
            self._set_stacked(self._force_stacked or self.contentsRect().width() < label_width + field_width + 12)

    def _set_stacked(self, stacked: bool) -> None:
        """Switch the same box layout between compact inline and wrapped rows."""
        if self.property("stacked") == stacked:
            return
        self.setProperty("stacked", stacked)
        self._row.setDirection(QBoxLayout.Direction.TopToBottom if stacked else QBoxLayout.Direction.LeftToRight)
        self._row.setContentsMargins(0, 2, 0, 6) if stacked else self._row.setContentsMargins(0, 5, 0, 5)
        self._row.setSpacing(6 if stacked else 12)
        if self._label is not None:
            self._row.setStretch(0, 0 if stacked else 1)
            self._row.setAlignment(self._label, Qt.AlignmentFlag(0) if stacked else Qt.AlignmentFlag.AlignVCenter)
        field_index = self._row.count() - 1
        self._row.setStretch(field_index, 1 if self._stretch and not stacked else 0)
        alignment = Qt.AlignmentFlag(0) if stacked or self._stretch else Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        self._row.setAlignment(self._field, alignment)
        self.style().unpolish(self)
        self.style().polish(self)
        self.updateGeometry()

    def track(self, widget: QWidget) -> None:
        """Follow caller-owned visibility changes without taking over their state."""
        widget.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Defer visibility synchronization until Qt finishes showing or hiding."""
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
        """Collapse an empty row and invalidate wrapped text geometry on restore."""
        try:
            # NOTE: the name argument must stay omitted — an explicit "" filters
            # by objectName and would drop named children such as SettingsToggle
            # ("settings_toggle"), collapsing toggle rows right after page show.
            children = self.findChildren(
                QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly
            )
            self.setVisible(any(child.isVisibleTo(self) for child in children))
            self._row.invalidate()
            self.updateGeometry()
        except RuntimeError:
            # The card's C++ object may already be gone during teardown.
            pass


class SettingsFormLayout(QFormLayout):
    """Apply common spacing and label placement to settings fields.

    Short controls use compact inline rows. Multiline editors, checkbox
    groups and pages opting into ``stacked_labels`` place their label above
    a full-width field. ``info=True`` marks non-editable explanatory rows.
    """

    _RIGHT_ALIGNED_TYPES = (QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox, QLabel, QPushButton)
    _STRETCH_TYPES = (QTextEdit, QPlainTextEdit, SettingsToggle)

    def __init__(self, parent=None, *, stacked_labels=False) -> None:
        """Apply the shared settings spacing while retaining QFormLayout's API."""
        super().__init__(parent)
        self._stacked_labels = stacked_labels
        self.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.setVerticalSpacing(6)

    def addRow(self, *args, info: bool = False) -> None:  # noqa: N802 - Qt API spelling
        """Add a labeled field or a full-width widget using the page's layout."""

        if not args or not isinstance(args[-1], QWidget):
            super().addRow(*args)
            return
        label = args[0] if len(args) == 2 and isinstance(args[0], QWidget) else None
        label_text = "" if label is not None else (
            str(args[0]) if len(args) == 2 and isinstance(args[1], QWidget) else ""
        )
        field = args[1] if len(args) == 2 and isinstance(args[1], QWidget) else args[0]
        mark_settings_controls(field)
        if isinstance(field, QPushButton):
            field.setFixedHeight(SETTINGS_SINGLE_LINE_HEIGHT)
            field.setMinimumWidth(64)
            field.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

        if label is None and label_text:
            label = QLabel(label_text)
        if isinstance(label, QLabel):
            label.setWordWrap(True)
            if label.buddy() is None:
                label.setBuddy(field)
            if not field.accessibleName():
                field.setAccessibleName(label.text())
        note = label is None and isinstance(field, QLabel)
        if note:
            field.setWordWrap(True)
        stacked = label is not None and (
            self._stacked_labels or isinstance(field, (QTextEdit, QPlainTextEdit)) or bool(field.findChildren(QCheckBox))
        )
        stretch = note or (info and len(args) == 1) or isinstance(field, self._STRETCH_TYPES) or not isinstance(field, self._RIGHT_ALIGNED_TYPES)
        if stacked or stretch:
            field.setSizePolicy(QSizePolicy.Policy.Expanding, field.sizePolicy().verticalPolicy())
        card = _RowCard(field, label, info=info or note, stacked=stacked, stretch=stretch)
        self.setWidget(self.rowCount(), QFormLayout.ItemRole.SpanningRole, card)
