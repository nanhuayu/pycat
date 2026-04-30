"""PyCat 统一图标管理。

当前实现使用内联 SVG + ``QSvgRenderer`` 生成透明背景的 ``QIcon``，避免 emoji、
系统字体与 icon font 在不同平台上的渲染差异。
"""

from __future__ import annotations

from functools import lru_cache

from PyQt6.QtCore import QByteArray, QRectF, Qt
from PyQt6.QtGui import QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer


def _stroke_path(d: str) -> str:
    return (
        f"<path d='{d}' fill='none' stroke='{{color}}' stroke-width='1.9' "
        "stroke-linecap='round' stroke-linejoin='round'/>"
    )


def _stroke_line(x1: float, y1: float, x2: float, y2: float) -> str:
    return (
        f"<line x1='{x1}' y1='{y1}' x2='{x2}' y2='{y2}' stroke='{{color}}' "
        "stroke-width='1.9' stroke-linecap='round'/>"
    )


def _stroke_circle(cx: float, cy: float, r: float) -> str:
    return f"<circle cx='{cx}' cy='{cy}' r='{r}' fill='none' stroke='{{color}}' stroke-width='1.9'/>"


def _stroke_rect(x: float, y: float, w: float, h: float, rx: float = 0.0) -> str:
    return f"<rect x='{x}' y='{y}' width='{w}' height='{h}' rx='{rx}' fill='none' stroke='{{color}}' stroke-width='1.9'/>"


def _fill_path(d: str) -> str:
    return f"<path d='{d}' fill='{{color}}' stroke='none'/>"


def _fill_rect(x: float, y: float, w: float, h: float, rx: float = 0.0) -> str:
    return f"<rect x='{x}' y='{y}' width='{w}' height='{h}' rx='{rx}' fill='{{color}}' stroke='none'/>"


_ICON_BODIES: dict[str, str] = {
    "folder": _stroke_path("M4.2 7.8h5.3l1.8 1.8h7.7a1.6 1.6 0 0 1 1.6 1.6v5.9A1.7 1.7 0 0 1 18.9 19H5.1A1.7 1.7 0 0 1 3.4 17.3V9.5A1.7 1.7 0 0 1 5.1 7.8z"),
    "file": _stroke_path("M7 3h7l4 4v14H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z")
    + _stroke_line(14, 3, 14, 8)
    + _stroke_line(14, 8, 18, 8),
    "plus": _stroke_line(12, 5, 12, 19) + _stroke_line(5, 12, 19, 12),
    "minus": _stroke_line(5, 12, 19, 12),
    "trash": _stroke_rect(7, 8, 10, 12, 2)
    + _stroke_line(5, 8, 19, 8)
    + _stroke_line(9, 5, 15, 5)
    + _stroke_line(10, 11, 10, 17)
    + _stroke_line(14, 11, 14, 17),
    "pen-to-square": _stroke_rect(4, 4, 16, 16, 2)
    + _stroke_path("M9 15l6-6 2 2-6 6-3 1 1-3z"),
    "download": _stroke_line(12, 4, 12, 15)
    + _stroke_line(8, 11, 12, 15)
    + _stroke_line(16, 11, 12, 15)
    + _stroke_line(5, 19, 19, 19),
    "upload": _stroke_line(12, 20, 12, 9)
    + _stroke_line(8, 13, 12, 9)
    + _stroke_line(16, 13, 12, 9)
    + _stroke_line(5, 5, 19, 5),
    "paper-plane": _stroke_path("M3 11.5L21 3l-7 18-2.8-6.2L3 11.5z") + _stroke_line(11.2, 14.8, 21, 3),
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
    "arrows-rotate": _stroke_path("M20 7v5h-5") + _stroke_path("M4 17v-5h5") + _stroke_path("M19 12a7 7 0 0 0-12-4") + _stroke_path("M5 12a7 7 0 0 0 12 4"),
    "stop": _fill_rect(7, 7, 10, 10, 2),
    "play": _fill_path("M8 6l10 6-10 6z"),
    "pause": _fill_rect(8, 6, 3.5, 12, 1) + _fill_rect(12.5, 6, 3.5, 12, 1),
    "gear": _stroke_circle(12, 12, 3.5)
    + _stroke_line(12, 3, 12, 6)
    + _stroke_line(12, 18, 12, 21)
    + _stroke_line(3, 12, 6, 12)
    + _stroke_line(18, 12, 21, 12)
    + _stroke_line(5.6, 5.6, 7.7, 7.7)
    + _stroke_line(16.3, 16.3, 18.4, 18.4)
    + _stroke_line(16.3, 7.7, 18.4, 5.6)
    + _stroke_line(5.6, 18.4, 7.7, 16.3),
    "sliders": _stroke_line(5, 7, 19, 7)
    + _stroke_circle(9, 7, 1.8)
    + _stroke_line(5, 12, 19, 12)
    + _stroke_circle(15, 12, 1.8)
    + _stroke_line(5, 17, 19, 17)
    + _stroke_circle(11, 17, 1.8),
    "comments": _stroke_rect(4, 5, 16, 11, 3) + _stroke_path("M8 16l-2 3 5-3"),
    "circle-info": _stroke_circle(12, 12, 8) + _stroke_line(12, 10.5, 12, 16) + _fill_rect(11.2, 6.5, 1.6, 1.6, 0.8),
    "circle-check": _stroke_circle(12, 12, 8) + _stroke_line(8, 12.5, 11, 15.5) + _stroke_line(11, 15.5, 16.5, 9.5),
    "circle-xmark": _stroke_circle(12, 12, 8) + _stroke_line(9, 9, 15, 15) + _stroke_line(15, 9, 9, 15),
    "check": _stroke_line(5.5, 12.5, 10, 17) + _stroke_line(10, 17, 18.5, 8.5),
    "xmark": _stroke_line(7, 7, 17, 17) + _stroke_line(17, 7, 7, 17),
    "magnifying-glass": _stroke_circle(11, 11, 5.5) + _stroke_line(15.5, 15.5, 20, 20),
    "plug": _stroke_line(9, 4, 9, 8) + _stroke_line(15, 4, 15, 8) + _stroke_rect(7, 8, 10, 6, 2) + _stroke_line(12, 14, 12, 20),
    "paperclip": _stroke_path("M8 12l6-6a4 4 0 1 1 6 6l-7 7a5 5 0 1 1-7-7l7-7"),
    "brain": _stroke_path("M10 6a3 3 0 0 0-3 3v1a3 3 0 0 0 1 5v1a3 3 0 0 0 6 0V9a3 3 0 0 0-4-3z")
    + _stroke_path("M14 6a3 3 0 0 1 3 3v1a3 3 0 0 1-1 5v1a3 3 0 0 1-6 0")
    + _stroke_line(12, 8, 12, 19),
    "eye": _stroke_path("M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6z") + _stroke_circle(12, 12, 2.5),
    "eye-slash": _stroke_path("M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6z") + _stroke_circle(12, 12, 2.5) + _stroke_line(4, 20, 20, 4),
    "shield-halved": _stroke_path("M12 3l7 3v5c0 5-3 8-7 10-4-2-7-5-7-10V6l7-3z") + _stroke_line(12, 6, 12, 19),
    "terminal": _stroke_rect(3, 5, 18, 14, 2) + _stroke_line(7, 10, 10, 12) + _stroke_line(10, 12, 7, 14) + _stroke_line(12.5, 14.5, 17, 14.5),
    "book-open": _stroke_path("M4 6.5A2.5 2.5 0 0 1 6.5 4H11v16H6.5A2.5 2.5 0 0 0 4 22z")
    + _stroke_path("M20 6.5A2.5 2.5 0 0 0 17.5 4H13v16h4.5A2.5 2.5 0 0 1 20 22z"),
    "wand-magic-sparkles": _stroke_line(5, 19, 14, 10)
    + _stroke_line(14, 10, 17, 13)
    + _stroke_line(16, 4, 16, 7)
    + _stroke_line(14.5, 5.5, 17.5, 5.5)
    + _stroke_line(7, 7, 7, 9.5)
    + _stroke_line(5.8, 8.2, 8.2, 8.2),
    "microchip": _stroke_rect(7, 7, 10, 10, 1.5)
    + _stroke_line(9, 3, 9, 7) + _stroke_line(12, 3, 12, 7) + _stroke_line(15, 3, 15, 7)
    + _stroke_line(9, 17, 9, 21) + _stroke_line(12, 17, 12, 21) + _stroke_line(15, 17, 15, 21)
    + _stroke_line(3, 9, 7, 9) + _stroke_line(3, 12, 7, 12) + _stroke_line(3, 15, 7, 15)
    + _stroke_line(17, 9, 21, 9) + _stroke_line(17, 12, 21, 12) + _stroke_line(17, 15, 21, 15),
}

_ICON_ALIASES: dict[str, str] = {
    "folder-open": "folder",
    "file-lines": "file",
    "copy": "file",
    "clone": "file",
    "file-import": "download",
    "file-export": "upload",
    "screwdriver-wrench": "gear",
    "wrench": "gear",
    "message": "comments",
    "robot": "shield-halved",
    "user": "circle-info",
    "users": "circle-info",
    "hourglass-half": "clock",
    "globe": "magnifying-glass",
    "network-wired": "magnifying-glass",
    "puzzle-piece": "sliders",
    "code": "terminal",
    "keyboard": "terminal",
    "book": "book-open",
    "palette": "gear",
    "sun": "gear",
    "moon": "gear",
    "lock": "shield-halved",
    "lock-open": "shield-halved",
    "key": "shield-halved",
    "image": "file",
    "camera": "file",
    "question": "circle-info",
    "link": "paperclip",
    "plug-circle-bolt": "plug",
    "graduation-cap": "book-open",
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


@lru_cache(maxsize=512)
def _render_pixmap(icon_name: str, size: int, color_name: str) -> QPixmap:
    body = _ICON_BODIES.get(_resolve_icon_name(icon_name), _ICON_BODIES.get("circle-info", ""))
    svg = _svg_document(body.format(color=color_name))
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    inset = 3.0 if _resolve_icon_name(icon_name) == "folder" and size >= 20 else (2.0 if size >= 20 else 1.5)
    renderer.render(painter, QRectF(inset, inset, max(1.0, size - inset * 2), max(1.0, size - inset * 2)))
    painter.end()
    return pixmap


def _build_icon(icon_name: str, *, color_name: str, base_size: int) -> QIcon:
    icon = QIcon()
    for size in sorted({max(16, base_size), max(20, int(base_size * 1.25)), max(24, int(base_size * 1.5))}):
        icon.addPixmap(_render_pixmap(icon_name, size, color_name))
    return icon


class Icons:
    """PyCat 统一图标标识符集合。"""

    # === 基础操作 ===
    SAVE = "floppy-disk"
    OPEN = "folder-open"
    FOLDER = "folder"
    FILE = "file"
    FILE_LINES = "file-lines"
    PLUS = "plus"
    MINUS = "minus"
    TRASH = "trash"
    COPY = "copy"
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
    PLAY = "play"
    PAUSE = "pause"

    # === 设置 / 配置 ===
    SETTINGS = "gear"
    TOOLS = "screwdriver-wrench"
    WRENCH = "wrench"
    SLIDERS = "sliders"

    # === 聊天 / 对话 ===
    CHAT = "comments"
    MESSAGE = "message"
    BOT = "robot"
    USER = "user"
    USERS = "users"
    BRAIN = "brain"

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
    PUZZLE = "puzzle-piece"
    EXTENSION = "puzzle-piece"

    # === 终端 / 代码 ===
    TERMINAL = "terminal"
    CODE = "code"
    KEYBOARD = "keyboard"

    # === 记忆 / 文档 ===
    BOOK = "book"
    BOOK_OPEN = "book-open"
    BOOKS = "book-open"
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
    CAMERA = "camera"

    # === 信息 / 关于 ===
    INFO = "circle-info"
    QUESTION = "question"
    LINK = "link"
    GITHUB = "link"  # 用 link 替代（品牌图标不在 Free Solid 字体中）
    WAND = "wand-magic-sparkles"

    # === 设置页专用 ===
    PAGE_MODELS = "microchip"
    PAGE_CHANNELS = "plug"
    PAGE_MCP = "plug-circle-bolt"
    PAGE_SEARCH = "magnifying-glass"
    PAGE_SKILLS = "graduation-cap"
    PAGE_TERMINAL_SETTINGS = "terminal"
    PAGE_CONTEXT = "book-open"
    PAGE_PROMPTS = "wand-magic-sparkles"
    PAGE_MODES = "puzzle-piece"
    PAGE_APPEARANCE = "gear"
    PAGE_ABOUT = "circle-info"
    PAGE_AGENTS = "shield-halved"

    # === 颜色常量 ===
    COLOR_PRIMARY = "#2563EB"
    COLOR_SUCCESS = "#16A34A"
    COLOR_ERROR = "#EF4444"
    COLOR_WARNING = "#F59E0B"
    COLOR_MUTED = "#64748B"

    # ---- 获取方法 ----

    @classmethod
    def get(cls, icon_name: str, *, color: str | None = None, scale_factor: float = 1.0) -> QIcon:
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
        if not icon_name:
            return QIcon()
        color_val = color or cls.COLOR_PRIMARY
        base_size = max(16, int(round(20 * scale_factor)))
        return _build_icon(icon_name, color_name=color_val, base_size=base_size)

    @classmethod
    def get_colored(cls, icon_name: str, color: str, *, scale_factor: float = 1.0) -> QIcon:
        """获取指定颜色的 QIcon。"""
        return cls.get(icon_name, color=color, scale_factor=scale_factor)

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
