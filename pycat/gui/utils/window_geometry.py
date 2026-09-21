"""Shared initial-size policies for top-level PyCat windows."""
from __future__ import annotations

from PyQt6.QtCore import QSize
from PyQt6.QtWidgets import QApplication, QWidget


MAIN_WINDOW_PREFERRED = (1080, 680)
MAIN_WINDOW_MINIMUM = (900, 600)
WORKBENCH_DIALOG_PREFERRED = (980, 640)
WORKBENCH_DIALOG_MINIMUM = (820, 540)


def apply_window_size(
    widget: QWidget,
    *,
    preferred: tuple[int, int],
    minimum: tuple[int, int],
    maximum: tuple[int, int] | None = None,
    parent_margin: int | None = None,
    screen_margin: int = 32,
) -> QSize:
    """Resize a window within its parent and screen while honoring a policy."""

    minimum_width, minimum_height = (max(1, int(value)) for value in minimum)
    width, height = (max(1, int(value)) for value in preferred)
    widget.setMinimumSize(minimum_width, minimum_height)

    if maximum is not None:
        width = min(width, int(maximum[0]))
        height = min(height, int(maximum[1]))

    parent = widget.parentWidget()
    if parent is not None and parent_margin is not None:
        width = min(width, max(minimum_width, parent.width() - int(parent_margin)))
        height = min(height, max(minimum_height, parent.height() - int(parent_margin)))

    screen = widget.screen() or QApplication.primaryScreen()
    if screen is not None:
        available = screen.availableGeometry()
        width = min(width, max(minimum_width, available.width() - int(screen_margin)))
        height = min(height, max(minimum_height, available.height() - int(screen_margin)))

    size = QSize(max(minimum_width, width), max(minimum_height, height))
    widget.resize(size)
    return size


def apply_workbench_dialog_size(widget: QWidget) -> QSize:
    return apply_window_size(
        widget,
        preferred=WORKBENCH_DIALOG_PREFERRED,
        minimum=WORKBENCH_DIALOG_MINIMUM,
        maximum=WORKBENCH_DIALOG_PREFERRED,
        parent_margin=40,
        screen_margin=32,
    )
