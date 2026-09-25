"""PyCat 统一图标管理。

当前实现使用内联 SVG + ``QSvgRenderer`` 生成透明背景的 ``QIcon``，避免 emoji、
系统字体与 icon font 在不同平台上的渲染差异。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from PyQt6.QtCore import QT_VERSION, QByteArray, QRectF, QSize, Qt
from PyQt6.QtGui import QIcon, QIconEngine, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QApplication

from pycat.gui.utils.theme import theme_tokens

if os.name == "nt" and not os.environ.get("WINDIR"):
    os.environ["WINDIR"] = os.environ.get("SystemRoot") or r"C:\Windows"


def _stroke_path(d: str, width: float = 2.15) -> str:
    return (
        f"<path d='{d}' fill='none' stroke='{{color}}' stroke-width='{width}' "
        "stroke-linecap='round' stroke-linejoin='round'/>"
    )


def _stroke_line(x1: float, y1: float, x2: float, y2: float, width: float = 2.15) -> str:
    return (
        f"<line x1='{x1}' y1='{y1}' x2='{x2}' y2='{y2}' stroke='{{color}}' "
        f"stroke-width='{width}' stroke-linecap='round'/>"
    )


def _stroke_circle(cx: float, cy: float, r: float, width: float = 2.15) -> str:
    return f"<circle cx='{cx}' cy='{cy}' r='{r}' fill='none' stroke='{{color}}' stroke-width='{width}'/>"


def _stroke_rect(x: float, y: float, w: float, h: float, rx: float = 0.0, width: float = 2.15) -> str:
    return f"<rect x='{x}' y='{y}' width='{w}' height='{h}' rx='{rx}' fill='none' stroke='{{color}}' stroke-width='{width}'/>"


def _fill_path(d: str) -> str:
    return f"<path d='{d}' fill='{{color}}' stroke='none'/>"


def _fill_rect(x: float, y: float, w: float, h: float, rx: float = 0.0) -> str:
    return f"<rect x='{x}' y='{y}' width='{w}' height='{h}' rx='{rx}' fill='{{color}}' stroke='none'/>"


def _fill_circle(cx: float, cy: float, r: float) -> str:
    return f"<circle cx='{cx}' cy='{cy}' r='{r}' fill='{{color}}' stroke='none'/>"


_ICON_BODIES: dict[str, str] = {
    "pycat": "<g transform='translate(2.7 2.4) scale(.036)'><path d='M96.19 172.74c0-19.65-9.7-52.91 11.83-91.12 43.38 12.02 49.07 35.75 75.25 53.66 51.81-12.96 92.67-12.96 144.48 0 26.18-17.91 31.87-41.63 75.25-53.66 21.53 38.21 11.83 71.47 11.83 91.12 32.71 36.53 38.79 78.45 37.17 107.83-2.1 38.63-19.91 100.95-79.13 121.12l36.59 96.97-100.72-79.06c-37.01 6.72-74.02 6.72-111.03 0-31.87-5.62-75.47-18.26-99.56-40.63-24.05-22.4-42.99-64.71-43.73-99.07-.71-34.36 2.36-73.5 39.37-107.12z' fill='none' stroke='{color}' stroke-width='34' stroke-linecap='round' stroke-linejoin='round'/><path d='M167.1 242.9l33.3 33.3-33.3 33.3M344.9 242.9l-33.3 33.3 33.3 33.3' fill='none' stroke='{color}' stroke-width='24' stroke-linecap='round' stroke-linejoin='round'/><circle cx='256' cy='297.5' r='20' fill='{color}'/></g>",
    "folder": _stroke_path("M3.8 8h6l1.6 1.8h8a1.8 1.8 0 0 1 1.8 1.8v5.2a2 2 0 0 1-2 2H4.8a2 2 0 0 1-2-2v-7a1.8 1.8 0 0 1 1.8-1.8z"),
    "file": _stroke_path("M7 3.8h6.5L18 8.3v11.2a1.8 1.8 0 0 1-1.8 1.8H7.8A1.8 1.8 0 0 1 6 19.5V5.6a1.8 1.8 0 0 1 1.8-1.8z")
    + _stroke_line(14, 3, 14, 8)
    + _stroke_line(14, 8, 18, 8),
    "file-lines": _stroke_path("M7 3.8h6.5L18 8.3v11.2a1.8 1.8 0 0 1-1.8 1.8H7.8A1.8 1.8 0 0 1 6 19.5V5.6a1.8 1.8 0 0 1 1.8-1.8z", 2.0)
    + _stroke_line(14, 3.8, 14, 8.2, 2.0)
    + _stroke_line(14, 8.2, 18, 8.2, 2.0)
    + _stroke_line(8.8, 12, 15.2, 12, 1.8)
    + _stroke_line(8.8, 15.3, 15.2, 15.3, 1.8),
    "floppy-disk": _stroke_rect(4, 3.5, 16, 17, 2.1, 2.2)
    + _stroke_path("M7.4 3.8v5.9h8.8V3.8", 2.15)
    + _stroke_rect(7.4, 13.8, 9.2, 6.4, 1.2, 2.05)
    + _stroke_line(9.8, 16.8, 14.2, 16.8, 1.95),
    "plus": _stroke_line(12, 5, 12, 19) + _stroke_line(5, 12, 19, 12),
    "chart-bars": _stroke_line(5, 12, 5, 20, 1.8) + _stroke_line(12, 4, 12, 20, 1.8) + _stroke_line(19, 8, 19, 20, 1.8),
    "ellipsis": "".join(f"<circle cx='{x}' cy='12' r='1.6' fill='{{color}}'/>" for x in (5, 12, 19)),
    "minus": _stroke_line(5, 12, 19, 12),
    "trash": _stroke_path("M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7M14 10v7"),
    "pen-to-square": _stroke_path("M5.5 18.5l3-.7 8.4-8.4a2 2 0 0 0-2.8-2.8L5.7 15l-.7 3a.5.5 0 0 0 .5.5z", 2.05)
    + _stroke_line(13.4, 7.4, 16.6, 10.6, 2.05),
    "download": _stroke_line(12, 3.8, 12, 15, 2.25)
    + _stroke_line(7.8, 10.8, 12, 15, 2.25)
    + _stroke_line(16.2, 10.8, 12, 15, 2.25)
    + _stroke_line(5, 19.2, 19, 19.2, 2.25),
    "upload": _stroke_line(12, 20.2, 12, 9, 2.25)
    + _stroke_line(7.8, 13.2, 12, 9, 2.25)
    + _stroke_line(16.2, 13.2, 12, 9, 2.25)
    + _stroke_line(5, 4.8, 19, 4.8, 2.25),
    "paper-plane": _fill_path("M3.7 11.2 20.1 3.6a1 1 0 0 1 1.3 1.3l-7.6 16.4a1 1 0 0 1-1.9-.1l-2-6.2-6.2-2a1 1 0 0 1-.1-1.8zM10.8 14l2 5.1 5.4-11.7z"),
    "arrow-right": _stroke_line(5, 12, 19, 12) + _stroke_line(13, 6, 19, 12) + _stroke_line(13, 18, 19, 12),
    "arrow-left": _stroke_line(5, 12, 19, 12) + _stroke_line(11, 6, 5, 12) + _stroke_line(11, 18, 5, 12),
    "arrow-up": _stroke_line(12, 5, 12, 19) + _stroke_line(6, 11, 12, 5) + _stroke_line(18, 11, 12, 5),
    "arrow-down": _stroke_line(12, 5, 12, 19) + _stroke_line(6, 13, 12, 19) + _stroke_line(18, 13, 12, 19),
    "chevron-right": _stroke_line(9, 7, 14, 12) + _stroke_line(14, 12, 9, 17),
    "chevron-left": _stroke_line(15, 7, 10, 12) + _stroke_line(10, 12, 15, 17),
    "chevron-up": _stroke_line(7, 14, 12, 9) + _stroke_line(12, 9, 17, 14),
    "chevron-down": _stroke_line(7, 10, 12, 15) + _stroke_line(12, 15, 17, 10),
    "angles-up": _stroke_line(7, 16.5, 12, 11.5) + _stroke_line(12, 11.5, 17, 16.5)
    + _stroke_line(7, 11.5, 12, 6.5) + _stroke_line(12, 6.5, 17, 11.5),
    "angles-down": _stroke_line(7, 7.5, 12, 12.5) + _stroke_line(12, 12.5, 17, 7.5)
    + _stroke_line(7, 12.5, 12, 17.5) + _stroke_line(12, 17.5, 17, 12.5),
    "arrows-rotate": _stroke_path("M20 12a8 8 0 1 0-2.34 5.66") + _stroke_path("M20 5v7h-7"),
    "stop": _fill_rect(5, 5, 14, 14, 1.4),
    "play": _fill_path("M8 6l10 6-10 6z"),
    "pause": _fill_rect(7.5, 5.8, 2.7, 12.4, 1.0) + _fill_rect(13.8, 5.8, 2.7, 12.4, 1.0),
    "gear": _stroke_path("M10 3h4l.5 2.6 1.7 1 2.5-.9 2 3.4-2 1.7v2.4l2 1.7-2 3.4-2.5-.9-1.7 1-.5 2.6h-4l-.5-2.6-1.7-1-2.5.9-2-3.4 2-1.7v-2.4l-2-1.7 2-3.4 2.5.9 1.7-1z", 2.0)
    + _stroke_circle(12, 12, 3.2, 2.0),
    "sliders": _stroke_line(5, 7, 19, 7)
    + _stroke_circle(9, 7, 1.8)
    + _stroke_line(5, 12, 19, 12)
    + _stroke_circle(15, 12, 1.8)
    + _stroke_line(5, 17, 19, 17)
    + _stroke_circle(11, 17, 1.8),
    "wrench": _stroke_path("M5 18.8l8.7-8.7", 2.55)
    + _stroke_path("M14.2 4.6a4.8 4.8 0 0 1 5.2 6.4l-2.9-2.9-3.2 3.2 2.9 2.9a4.8 4.8 0 0 1-6.4-5.2", 2.4)
    + _stroke_circle(5, 18.8, 1.25, 2.2),
    "toolbox": _stroke_rect(3, 8, 18, 12, 2, 2.0)
    + _stroke_path("M8 8V5h8v3M3 13h18M10 12v3h4v-3", 2.0),
    "panel-left": _stroke_rect(4, 4, 16, 16, 2) + _stroke_line(9, 4, 9, 20),
    "panel-right": _stroke_rect(4, 4, 16, 16, 2) + _stroke_line(15, 4, 15, 20),
    "panel": _stroke_rect(4, 5, 16, 14, 2) + _stroke_line(4, 10, 20, 10),
    "external-link": _stroke_rect(5, 8, 11, 11, 2)
    + _stroke_line(12, 5, 19, 5)
    + _stroke_line(19, 5, 19, 12)
    + _stroke_line(11, 13, 19, 5),
    "comments": _stroke_rect(4, 5, 16, 11, 3, 2.15) + _stroke_path("M8 16l-2 3 5-3", 2.15),
    "copy": _stroke_rect(8, 5, 9.7, 9.7, 1.5, 2.0) + _stroke_rect(5.4, 8.4, 9.7, 9.7, 1.5, 2.0),
    "pin": _stroke_path("M8 3h8l-1 7 3 4H6l3-4z") + _stroke_line(12, 14, 12, 21),
    "scan-text": _stroke_path("M8 3H3v5M16 3h5v5M3 16v5h5M21 16v5h-5")
    + _stroke_line(8, 9, 16, 9) + _stroke_line(8, 12, 16, 12) + _stroke_line(8, 15, 13, 15),
    "fit-image": _stroke_path("M8 3H3v5M16 3h5v5M3 16v5h5M21 16v5h-5") + _stroke_rect(7, 7, 10, 10, 1),
    "circle-info": _stroke_circle(12, 12, 8) + _stroke_line(12, 10.5, 12, 16) + _fill_rect(11.2, 6.5, 1.6, 1.6, 0.8),
    "question": _stroke_circle(12, 12, 8, 2.0)
    + _stroke_path("M9.3 9a2.7 2.7 0 0 1 5.4 0c0 2-2.7 2-2.7 4", 2.0)
    + _fill_circle(12, 16.5, 1.0),
    "circle-check": _stroke_circle(12, 12, 8) + _stroke_line(8, 12.5, 11, 15.5) + _stroke_line(11, 15.5, 16.5, 9.5),
    "circle-xmark": _stroke_circle(12, 12, 8) + _stroke_line(9, 9, 15, 15) + _stroke_line(15, 9, 9, 15),
    "check": _stroke_line(5.5, 12.5, 10, 17) + _stroke_line(10, 17, 18.5, 8.5),
    "xmark": _stroke_line(7, 7, 17, 17) + _stroke_line(17, 7, 7, 17),
    "magnifying-glass": _stroke_circle(10.8, 10.8, 5.3) + _stroke_line(15, 15, 20, 20),
    "plug": _stroke_line(9, 4, 9, 8) + _stroke_line(15, 4, 15, 8) + _stroke_rect(7, 8, 10, 6, 2) + _stroke_line(12, 14, 12, 20),
    "server": _stroke_rect(4.5, 5, 15, 5.8, 1.4, 2.0)
    + _stroke_rect(4.5, 13.2, 15, 5.8, 1.4, 2.0)
    + _fill_circle(8, 7.9, 0.9)
    + _fill_circle(8, 16.1, 0.9)
    + _stroke_line(11, 7.9, 16, 7.9, 1.8)
    + _stroke_line(11, 16.1, 16, 16.1, 1.8),
    "paperclip": _stroke_path("M8 12l6-6a4 4 0 1 1 6 6l-7 7a5 5 0 1 1-7-7l7-7"),
    "link": _stroke_path("M10 7l2-2a4.2 4.2 0 0 1 6 6l-2 2M14 17l-2 2a4.2 4.2 0 0 1-6-6l2-2M8.5 15.5l7-7", 2.0),
    "lightbulb": _stroke_path("M7.5 10.1a4.5 4.5 0 1 1 9 0c0 1.7-.85 2.75-1.9 3.8-.58.58-.9 1.18-.98 2.05h-3.24c-.08-.87-.4-1.47-.98-2.05-1.05-1.05-1.9-2.1-1.9-3.8z", 2.25)
    + _stroke_line(9.4, 18, 14.6, 18, 2.25)
    + _stroke_line(10.2, 21, 13.8, 21, 2.25),
    "brain": _stroke_path("M10 6a3 3 0 0 0-3 3v1a3 3 0 0 0 1 5v1a3 3 0 0 0 6 0V9a3 3 0 0 0-4-3z")
    + _stroke_path("M14 6a3 3 0 0 1 3 3v1a3 3 0 0 1-1 5v1a3 3 0 0 1-6 0")
    + _stroke_line(12, 8, 12, 19),
    "eye": _stroke_path("M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6z") + _stroke_circle(12, 12, 2.5),
    "eye-slash": _stroke_path("M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6z") + _stroke_circle(12, 12, 2.5) + _stroke_line(4, 20, 20, 4),
    "shield-halved": _stroke_path("M12 2.8l7.4 3.1v5.4c0 5.3-3.2 8.3-7.4 10.1-4.2-1.8-7.4-4.8-7.4-10.1V5.9L12 2.8z", 2.25)
    + _stroke_line(12, 6.2, 12, 18.7, 2.15),
    "lock": _stroke_rect(5.5, 10.2, 13, 9.2, 2, 2.0) + _stroke_path("M8.5 10.2V8a3.5 3.5 0 0 1 7 0v2.2", 2.0),
    "lock-open": _stroke_rect(5.5, 10.2, 13, 9.2, 2, 2.0) + _stroke_path("M8.5 10.2V8a3.5 3.5 0 0 1 6.4-2", 2.0),
    "key": _stroke_circle(8.2, 12, 3.2, 2.0) + _stroke_line(11.4, 12, 20, 12, 2.0) + _stroke_line(16.2, 12, 16.2, 15.2, 2.0) + _stroke_line(19.2, 12, 19.2, 14.2, 2.0),
    "robot": _stroke_rect(5.5, 8, 13, 9.5, 2.2, 2.0)
    + _stroke_line(12, 5, 12, 8, 2.0)
    + _fill_circle(12, 4.3, 1.1)
    + _fill_circle(9.2, 12.1, 1.0)
    + _fill_circle(14.8, 12.1, 1.0)
    + _stroke_line(9.5, 15.2, 14.5, 15.2, 1.8),
    "user": _stroke_circle(12, 8, 3.3, 2.0) + _stroke_path("M5.8 20a6.2 6.2 0 0 1 12.4 0", 2.0),
    "users": _stroke_circle(9.4, 8.8, 2.7, 1.9)
    + _stroke_path("M4.7 19a5 5 0 0 1 9.4 0", 1.9)
    + _stroke_path("M15 7.4a2.6 2.6 0 0 1 0 5", 1.9)
    + _stroke_path("M15.2 15a4.7 4.7 0 0 1 4.1 4", 1.9),
    "terminal": _stroke_rect(3, 5, 18, 14, 2) + _stroke_line(7, 10, 10, 12) + _stroke_line(10, 12, 7, 14) + _stroke_line(12.5, 14.5, 17, 14.5),
    "keyboard": _stroke_rect(2, 5, 20, 14, 2, 1.8)
    + "".join(_fill_rect(x, y, 1.6, 1.6, 0.3) for y in (8, 11.5) for x in (5, 9, 13, 17))
    + _stroke_line(7, 16, 17, 16, 1.8),
    "code": _stroke_path("M7 7l-5 5 5 5M17 7l5 5-5 5M14 4l-4 16", 2.0),
    "book-open": _stroke_path("M4 6.5A2.5 2.5 0 0 1 6.5 4H11v16H6.5A2.5 2.5 0 0 0 4 22z")
    + _stroke_path("M20 6.5A2.5 2.5 0 0 0 17.5 4H13v16h4.5A2.5 2.5 0 0 1 20 22z"),
    "library": _stroke_rect(3, 4, 4, 16, 0.8, 2.0)
    + _stroke_rect(8, 4, 4, 16, 0.8, 2.0)
    + _stroke_path("M14 5l4-1 4 15-4 1z", 2.0),
    "wand-magic-sparkles": _stroke_line(5, 19, 14, 10)
    + _stroke_line(14, 10, 17, 13)
    + _stroke_line(16, 4, 16, 7)
    + _stroke_line(14.5, 5.5, 17.5, 5.5)
    + _stroke_line(7, 7, 7, 9.5)
    + _stroke_line(5.8, 8.2, 8.2, 8.2),
    "palette": _stroke_path("M12 4.5a7.5 7.5 0 0 0 0 15h1.2a1.8 1.8 0 0 0 1.2-3.1 1.5 1.5 0 0 1 1-2.6H16a4 4 0 0 0 4-4c0-3-3.3-5.3-8-5.3z", 2.0)
    + _fill_circle(8.5, 10.5, 1.0)
    + _fill_circle(11.2, 8.2, 1.0)
    + _fill_circle(14.8, 8.8, 1.0),
    "sun": _stroke_circle(12, 12, 3.5, 2.0)
    + _stroke_path("M12 3.5v1.7M12 18.8v1.7M3.5 12h1.7M18.8 12h1.7M6 6l1.2 1.2M16.8 16.8 18 18M18 6l-1.2 1.2M7.2 16.8 6 18", 2.0),
    "moon": _stroke_path("M18.7 15.4A7.4 7.4 0 0 1 8.6 5.3 7.5 7.5 0 1 0 18.7 15.4z", 2.0),
    "clock": _stroke_circle(12, 12, 8, 2.0) + _stroke_line(12, 7.5, 12, 12, 2.0) + _stroke_line(12, 12, 15.5, 14.2, 2.0),
    "spinner": _stroke_path("M12 4a8 8 0 1 1-7 4.1", 2.1) + _stroke_line(5, 4.6, 5, 8.1, 2.1) + _stroke_line(5, 8.1, 8.5, 8.1, 2.1),
    "globe": _stroke_circle(12, 12, 8, 2.0)
    + _stroke_path("M4.5 12h15M12 4a12 12 0 0 1 0 16M12 4a12 12 0 0 0 0 16", 1.75),
    "network-wired": _stroke_rect(4.5, 5, 5.5, 4.5, 1, 1.8)
    + _stroke_rect(14, 5, 5.5, 4.5, 1, 1.8)
    + _stroke_rect(9.25, 15, 5.5, 4.5, 1, 1.8)
    + _stroke_line(7.25, 9.5, 12, 15, 1.8)
    + _stroke_line(16.75, 9.5, 12, 15, 1.8),
    "puzzle-piece": _stroke_path("M7 4.5h4.1a2 2 0 1 0 1.8 0H17v4.1a2 2 0 1 1 0 3.8v4.1h-4.1a2 2 0 1 0-3.8 0H7v-4.1a2 2 0 1 1 0-3.8z", 2.0),
    "graduation-cap": _stroke_path("M3.5 9.5 12 5.2l8.5 4.3-8.5 4.3z", 2.0)
    + _stroke_path("M7.2 12v3.2c1.4 1.2 3 1.8 4.8 1.8s3.4-.6 4.8-1.8V12", 2.0)
    + _stroke_line(20.5, 9.5, 20.5, 15.5, 1.8),
    "image": _stroke_rect(4, 5, 16, 14, 2, 2.0)
    + _fill_circle(9, 9.5, 1.3)
    + _stroke_path("M5.5 17l4.8-4.8 3.2 3.2 1.8-1.8L18.5 17", 2.0),
    "camera": _stroke_rect(4, 7.5, 16, 11, 2, 2.0)
    + _stroke_path("M8.5 7.5 10 5.5h4l1.5 2", 2.0)
    + _stroke_circle(12, 13, 3.0, 2.0),
    "microchip": _stroke_rect(6, 6, 12, 12, 2, 2.0)
    + _stroke_rect(9, 9, 6, 6, 0.6, 1.6)
    + "".join(_stroke_path(f"M{p} 3v3M{p} 18v3M3 {p}h3M18 {p}h3", 1.8) for p in (9, 15)),
    "volume-high": _stroke_path("M3 9h4l5-4v14l-5-4H3zM16 9a5 5 0 0 1 0 6M19 6a9 9 0 0 1 0 12", 2.0),
}

_ICON_ALIASES: dict[str, str] = {
    "folder-open": "folder",
    "clone": "copy",
    "stop-filled": "stop",
    "file-import": "download",
    "file-export": "upload",
    "message": "comments",
    "file-text": "file-lines",
    "panel-left-close": "panel-left",
    "panel-right-close": "panel-right",
    "sidebar-left": "panel-left",
    "sidebar-right": "panel-right",
    "open-external": "external-link",
    "hourglass-half": "clock",
    "book": "book-open",
    "plug-circle-bolt": "server",
    "server-rack": "server",
}

def _svg_document(body: str) -> str:
    return (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'>"
        f"{body}"
        "</svg>"
    )


def _resolve_icon_name(icon_name: str) -> str:
    current = str(icon_name or "").strip()
    visited: set[str] = set()
    while current in _ICON_ALIASES and current not in visited:
        visited.add(current)
        current = _ICON_ALIASES[current]
    return current


def _icon_inset(icon_name: str, size: int) -> float:
    """Keep small toolbar icons readable without clipping the outer stroke."""
    compact = {
        "pycat",
        "paper-plane",
        "play",
        "pause",
        "stop",
        "plus",
        "minus",
        "check",
        "xmark",
        "trash",
        "copy",
        "pen-to-square",
        "floppy-disk",
        "gear",
        "microchip",
        "lightbulb",
        "wrench",
        "shield-halved",
        "server",
    }
    if icon_name in compact:
        return 0.15 if size <= 20 else 0.45
    return 0.35 if size <= 20 else 0.7


@lru_cache(maxsize=512)
def _render_pixmap(icon_name: str, size: int, color_name: str) -> QPixmap:
    resolved = _resolve_icon_name(icon_name)
    body = _ICON_BODIES.get(resolved, _ICON_BODIES.get("circle-info", ""))
    svg = _svg_document(body.format(color=color_name))
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    inset = _icon_inset(resolved, size)
    renderer.render(painter, QRectF(inset, inset, max(1.0, size - inset * 2), max(1.0, size - inset * 2)))
    painter.end()
    return pixmap


class _SemanticColor(str):
    """A usable color string that retains its theme role for icon rendering."""

    def __new__(cls, value: str, role: str):
        color = super().__new__(cls, value)
        color.role = role
        return color


@lru_cache(maxsize=32)
def _icon_colors(theme: str, accent: str) -> dict[str, str]:
    return theme_tokens(theme, accent).colors


def _current_icon_colors() -> dict[str, str]:
    app = QApplication.instance()
    return _icon_colors(str(app.property("theme") or "light") if app else "light",
                        str(app.property("accent") or "") if app else "")


class _SvgIconEngine(QIconEngine):
    """Render the same icon against the current palette without rebuilding widgets."""

    def __init__(self, name: str, color: str, base_size: int):
        super().__init__()
        self.name = name
        self.color = color
        self.base_size = base_size

    def clone(self):
        return _SvgIconEngine(self.name, self.color, self.base_size)

    def isNull(self):
        return False

    def iconName(self):
        return self.name

    def actualSize(self, size, mode, state):
        side = min(size.width(), size.height())
        return QSize(side, side)

    def availableSizes(self, mode, state):
        return [QSize(side, side) for side in sorted({16, 18, 20, 24, self.base_size})]

    def pixmap(self, size, mode, state):
        if size.isEmpty():
            return QPixmap()
        colors = _current_icon_colors()
        color = colors["disabled"] if mode == QIcon.Mode.Disabled else (
            colors[self.color.role] if isinstance(self.color, _SemanticColor) else self.color)
        return QPixmap(_render_pixmap(self.name, min(size.width(), size.height()), str(color)))

    def scaledPixmap(self, size, mode, state, scale):
        # QIcon passed physical sizes before Qt 6.8, logical sizes thereafter.
        pixels = size * scale if QT_VERSION >= 0x060800 else size
        result = self.pixmap(pixels, mode, state)
        result.setDevicePixelRatio(scale)
        return result

    def paint(self, painter, rect, mode, state):
        scale = painter.device().devicePixelRatioF()
        result = self.pixmap(rect.size() * scale, mode, state)
        painter.drawPixmap(rect, result)


class _ThemeColor:
    def __init__(self, name: str):
        self.name = name

    def __get__(self, instance, owner) -> str:
        return _SemanticColor(_current_icon_colors()[self.name].upper(), self.name)


class Icons:
    """PyCat 统一图标标识符集合。"""

    # === 基础操作 ===
    SAVE = "floppy-disk"
    OPEN = "folder-open"
    FOLDER = "folder"
    FILE = "file"
    FILE_LINES = "file-lines"
    PLUS = "plus"
    MORE = "ellipsis"
    MINUS = "minus"
    TRASH = "trash"
    COPY = "copy"
    PIN = "pin"
    OCR = "scan-text"
    FIT_IMAGE = "fit-image"
    CLONE = "clone"
    EDIT = "pen-to-square"
    DOWNLOAD = "download"
    UPLOAD = "upload"
    IMPORT = "file-import"
    EXPORT = "file-export"

    # === 导航 / 动作 ===
    SEND = "paper-plane"
    ARROW_RIGHT = "arrow-right"
    ARROW_LEFT = "arrow-left"
    ARROW_UP = "arrow-up"
    ARROW_DOWN = "arrow-down"
    CHEVRON_RIGHT = "chevron-right"
    CHEVRON_LEFT = "chevron-left"
    CHEVRON_UP = "chevron-up"
    CHEVRON_DOWN = "chevron-down"
    ANGLES_UP = "angles-up"
    ANGLES_DOWN = "angles-down"
    REFRESH = "arrows-rotate"
    STOP = "stop"
    STOP_FILLED = "stop-filled"
    PLAY = "play"
    PAUSE = "pause"

    # === 设置 / 配置 ===
    SETTINGS = "gear"
    TOOLS = "toolbox"
    WRENCH = "wrench"
    SLIDERS = "sliders"
    CHART_BARS = "chart-bars"
    PANEL = "panel"
    PANEL_LEFT = "panel-left"
    PANEL_RIGHT = "panel-right"
    EXTERNAL_OPEN = "external-link"
    MODEL = "microchip"

    # === 聊天 / 对话 ===
    CHAT = "comments"
    PYCAT = "pycat"
    MESSAGE = "message"
    BOT = "robot"
    USER = "user"
    USERS = "users"
    BRAIN = "brain"
    LIGHTBULB = "lightbulb"
    THINKING = "lightbulb"

    # === 状态指示 ===
    CHECK = "check"
    XMARK = "xmark"
    CIRCLE_INFO = "circle-info"
    CIRCLE_XMARK = "circle-xmark"
    CIRCLE_CHECK = "circle-check"
    SPINNER = "spinner"
    CLOCK = "clock"
    HOURGLASS_HALF = "hourglass-half"

    # === 搜索 / 发现 ===
    SEARCH = "magnifying-glass"
    GLOBE = "globe"
    NETWORK = "network-wired"

    # === 连接 / 插件 ===
    PLUG = "plug"
    SERVER = "server"
    PUZZLE = "puzzle-piece"
    EXTENSION = "puzzle-piece"

    # === 终端 / 代码 ===
    TERMINAL = "terminal"
    CODE = "code"
    KEYBOARD = "keyboard"

    # === 记忆 / 文档 ===
    BOOK = "book"
    BOOK_OPEN = "book-open"
    BOOKS = "library"
    MEMORY = "brain"
    DOCUMENT = "file-lines"

    # === 外观 / 主题 ===
    PALETTE = "palette"
    EYE = "eye"
    EYE_SLASH = "eye-slash"
    SUN = "sun"
    MOON = "moon"

    # === 安全 / 权限 ===
    LOCK = "lock"
    UNLOCK = "lock-open"
    SHIELD = "shield-halved"
    KEY = "key"

    # === 附件 ===
    PAPERCLIP = "paperclip"
    IMAGE = "image"
    AUDIO = "volume-high"
    CAMERA = "camera"

    # === 信息 / 关于 ===
    INFO = "circle-info"
    QUESTION = "question"
    LINK = "link"
    GITHUB = "link"  # Repository link; third-party brand marks are not part of this set.
    WAND = "wand-magic-sparkles"

    # === 设置页专用 ===
    PAGE_MODELS = "microchip"
    PAGE_CHANNELS = "plug"
    PAGE_MCP = "server"
    PAGE_SEARCH = "magnifying-glass"
    PAGE_SKILLS = "graduation-cap"
    PAGE_TERMINAL_SETTINGS = "terminal"
    PAGE_CONTEXT = "book-open"
    PAGE_CAPABILITIES = "wand-magic-sparkles"
    PAGE_MODES = "puzzle-piece"
    PAGE_APPEARANCE = "gear"
    PAGE_ABOUT = "circle-info"
    PAGE_AGENTS = "shield-halved"

    # === 颜色常量 ===
    COLOR_PRIMARY = _ThemeColor("primary")
    COLOR_SUCCESS = _ThemeColor("success")
    COLOR_ERROR = _ThemeColor("error")
    COLOR_WARNING = _ThemeColor("warning")
    COLOR_MUTED = _ThemeColor("muted")
    SIZE_TOOL = 18
    SIZE_NAV = 18
    SIZE_EMPTY_HERO = 30
    SIZE_SETTINGS_NAV = 18

    # ---- 获取方法 ----

    @staticmethod
    def brand() -> QIcon:
        return QIcon(str(Path(__file__).resolve().parents[2] / "assets" / "pycat.svg"))

    @classmethod
    def get(
        cls,
        icon_name: str,
        *,
        color: str | None = None,
        scale_factor: float = 1.0,
    ) -> QIcon:
        """获取 QIcon 实例。

        Args:
            icon_name: 图标标识符，如 ``Icons.SEND``。
            color: 颜色（如 ``'#4A90D9'``），默认使用 COLOR_PRIMARY。
            scale_factor: 缩放系数。

        Returns:
            QIcon 实例。
        """
        if not icon_name:
            return QIcon()
        color_val = color or cls.COLOR_PRIMARY
        resolved = _resolve_icon_name(icon_name)
        return QIcon(_SvgIconEngine(resolved if resolved in _ICON_BODIES else Icons.CIRCLE_INFO,
                                   color_val, max(16, int(round(20 * scale_factor)))))

    @classmethod
    def get_success(cls, icon_name: str, *, scale_factor: float = 1.0) -> QIcon:
        return cls.get(icon_name, color=cls.COLOR_SUCCESS, scale_factor=scale_factor)

    @classmethod
    def get_error(cls, icon_name: str, *, scale_factor: float = 1.0) -> QIcon:
        return cls.get(icon_name, color=cls.COLOR_ERROR, scale_factor=scale_factor)

    @classmethod
    def get_warning(cls, icon_name: str, *, scale_factor: float = 1.0) -> QIcon:
        return cls.get(icon_name, color=cls.COLOR_WARNING, scale_factor=scale_factor)

    @classmethod
    def get_muted(cls, icon_name: str, *, scale_factor: float = 1.0) -> QIcon:
        return cls.get(icon_name, color=cls.COLOR_MUTED, scale_factor=scale_factor)
