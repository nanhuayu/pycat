from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QComboBox, QListView


def configure_combo_popup(
    combo: QComboBox,
    *,
    popup_minimum_width: int | None = None,
    object_name: str = "combo_popup_view",
) -> QComboBox:
    """Attach a styled ``QListView`` popup to a combo box.

    Using an explicit ``QListView`` keeps popup rendering consistent across
    platforms/themes and avoids native popup palette glitches in settings
    dialogs on Windows light theme.
    """

    view = QListView(combo)
    view.setObjectName(object_name)
    try:
        view.setTextElideMode(Qt.TextElideMode.ElideMiddle)
    except Exception:
        pass
    if popup_minimum_width is not None:
        try:
            view.setMinimumWidth(max(0, int(popup_minimum_width)))
        except Exception:
            pass

    combo.setView(view)
    try:
        combo.view().setObjectName(object_name)
        combo.view().setTextElideMode(Qt.TextElideMode.ElideMiddle)
    except Exception:
        pass
    return combo
