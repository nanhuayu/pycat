"""Qt screen capture adapter; no dialogs, clipboard or Agent execution.

Call on the GUI thread. Regions use logical pixels relative to one screen;
returned images use device pixels. Mouse and precise region selection share
the same conversion. Agent capture remains owned by its authorized MCP driver.
"""
from __future__ import annotations

import math

from PyQt6.QtCore import QRect, QRectF
from PyQt6.QtGui import QGuiApplication, QImage


def crop_capture(image: QImage, viewport: QRect, region: QRect | QRectF) -> QImage:
    """Map screen-local logical coordinates to the captured device pixels."""
    rect = QRectF(region)
    if image.isNull() or viewport.isEmpty() or rect.isEmpty() or not QRectF(viewport).contains(rect):
        raise ValueError('截图区域超出所选屏幕，请调整坐标或尺寸。')
    sx, sy = image.width() / viewport.width(), image.height() / viewport.height()
    left, top = math.floor((rect.left() - viewport.left()) * sx), math.floor((rect.top() - viewport.top()) * sy)
    right = math.ceil((rect.left() - viewport.left() + rect.width()) * sx)
    bottom = math.ceil((rect.top() - viewport.top() + rect.height()) * sy)
    cropped = image.copy(QRect(left, top, right - left, bottom - top).intersected(image.rect()))
    cropped.setDevicePixelRatio(1)
    return cropped


def capture_screens() -> list[tuple[QImage, QRect]]:
    snapshots = []
    for screen in QGuiApplication.screens():
        image = screen.grabWindow(0).toImage()
        if not image.isNull():
            snapshots.append((image, screen.geometry()))
    if not snapshots:
        raise ValueError('当前桌面无法截图，请检查系统屏幕录制权限')
    return snapshots
