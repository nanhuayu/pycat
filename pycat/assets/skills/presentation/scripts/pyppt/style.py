"""样式工具：CSS 值解析、颜色、渐变、边框、间距。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

NAMED_COLORS = {
    "white": "#FFFFFF", "black": "#000000", "red": "#FF0000", "green": "#008000",
    "blue": "#0000FF", "gray": "#808080", "grey": "#808080", "silver": "#C0C0C0",
    "orange": "#FFA500", "purple": "#800080", "yellow": "#FFFF00", "navy": "#000080",
    "teal": "#008080", "maroon": "#800000", "olive": "#808000", "lime": "#00FF00",
    "aqua": "#00FFFF", "cyan": "#00FFFF", "fuchsia": "#FF00FF", "magenta": "#FF00FF",
    "transparent": "transparent", "none": "transparent",
}

_RGB_RE = re.compile(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)", re.I)
_HEX_RE = re.compile(r"#([0-9a-fA-F]{3,8})\b")
_PX_RE = re.compile(r"^\s*(-?[\d.]+)\s*(px|pt|%)?\s*$")
_GRADIENT_RE = re.compile(r"^(linear|radial)-gradient\((.*)\)$", re.I | re.S)
_STOP_RE = re.compile(r"(#[0-9a-fA-F]{3,8}|rgba?\([^)]*\)|[a-zA-Z]+)\s*([\d.]+)?%?")


@dataclass
class Color:
    """归一化颜色：hex 字符串 + alpha(0~1)。"""

    hex: str = ""
    alpha: float = 1.0
    transparent: bool = False

    @property
    def rgb_hex(self) -> str:
        return self.hex or "000000"

    def is_visible(self) -> bool:
        return not self.transparent and self.alpha > 0.005


def parse_color(value: Any) -> Color:
    """解析 '#RRGGBB' / '#RGB' / '#RRGGBBAA' / rgb()/rgba() / 命名色。"""
    if value is None:
        return Color(transparent=True)
    if isinstance(value, (int, float)):
        # 视为灰度
        v = max(0, min(255, int(value)))
        return Color(hex=f"{v:02X}{v:02X}{v:02X}")
    text = str(value).strip()
    if not text:
        return Color(transparent=True)
    low = text.lower()
    if low in NAMED_COLORS:
        mapped = NAMED_COLORS[low]
        if mapped == "transparent":
            return Color(transparent=True)
        return parse_color(mapped)

    m = _RGB_RE.match(text)
    if m:
        r, g, b = (max(0, min(255, int(float(m.group(i))))) for i in (1, 2, 3))
        alpha = float(m.group(4)) if m.group(4) is not None else 1.0
        return Color(hex=f"{r:02X}{g:02X}{b:02X}", alpha=max(0.0, min(1.0, alpha)))

    m = _HEX_RE.search(text)
    if m:
        digits = m.group(1)
        if len(digits) == 3:
            digits = "".join(ch * 2 for ch in digits)
        if len(digits) == 8:  # RRGGBBAA
            alpha = int(digits[6:8], 16) / 255.0
            return Color(hex=digits[:6].upper(), alpha=round(alpha, 3))
        if len(digits) == 6:
            return Color(hex=digits.upper())
        if len(digits) == 4:  # RGBA
            alpha = int(digits[3] * 2, 16) / 255.0
            return Color(hex="".join(c * 2 for c in digits[:3]).upper(), alpha=round(alpha, 3))
    return Color(transparent=True)


@dataclass
class Gradient:
    kind: str                  # linear | radial
    angle_deg: float           # 90 表示从上到下（CSS 约定）
    stops: List[Tuple[Color, float]]

    def first(self) -> Color:
        return self.stops[0][0] if self.stops else Color(transparent=True)

    def last(self) -> Color:
        return self.stops[-1][0] if self.stops else Color(transparent=True)


def parse_gradient(value: Any) -> Optional[Gradient]:
    """解析 'linear-gradient(135deg, #3b82f6 0%, #8b5cf6 100%)'。"""
    if not isinstance(value, str):
        return None
    m = _GRADIENT_RE.match(value.strip())
    if not m:
        return None
    kind = m.group(1).lower()
    body = m.group(2)
    angle = 180.0
    parts = [p.strip() for p in body.split(",") if p.strip()]
    if parts:
        head = parts[0]
        am = re.match(r"^(-?[\d.]+)deg$", head, re.I)
        if am:
            angle = float(am.group(1))
            parts = parts[1:]
        elif head.lower().startswith(("to ", "circle", "ellipse", "at ")):
            if head.lower().startswith("to "):
                dirs = {"to top": 0.0, "to right": 90.0, "to bottom": 180.0, "to left": 270.0}
                angle = dirs.get(head.lower(), 180.0)
            parts = parts[1:]
    stops: List[Tuple[Color, float]] = []
    for part in parts:
        sm = _STOP_RE.match(part)
        if not sm:
            continue
        color = parse_color(sm.group(1))
        pos = float(sm.group(2)) / 100.0 if sm.group(2) is not None else None
        stops.append((color, pos if pos is not None else -1.0))
    if not stops:
        return None
    # 补齐缺省位置
    n = len(stops)
    for i, (color, pos) in enumerate(stops):
        if pos < 0:
            stops[i] = (color, i / max(1, n - 1))
    stops.sort(key=lambda x: x[1])
    return Gradient(kind=kind, angle_deg=angle, stops=stops)


def to_css_angle(pptx_angle: float) -> float:
    """python-pptx 渐变角度为 0=从左到右、顺时针；CSS 0=从下到上。

    这里做一个保守映射：CSS 角度 a → OOXML 角度 (90 - a) % 360。
    """
    return (90.0 - float(pptx_angle)) % 360.0


def parse_length(value: Any) -> Tuple[Optional[float], Optional[float]]:
    """返回 (数值, 百分比)。'100%' → (None, 100)；24 → (24, None)。"""
    if value is None:
        return None, None
    if isinstance(value, (int, float)):
        return float(value), None
    text = str(value).strip()
    m = _PX_RE.match(text)
    if not m:
        return None, None
    num = float(m.group(1))
    unit = (m.group(2) or "").lower()
    if unit == "%":
        return None, num
    if unit == "pt":
        return num / 0.75, None  # pt → px
    return num, None


def resolve_length(value: Any, base: float, default: Optional[float] = None) -> Optional[float]:
    """把 '50%' / 320 / '12pt' 解析为基于 base 的像素值。"""
    num, pct = parse_length(value)
    if pct is not None:
        return base * pct / 100.0
    if num is not None:
        return num
    return default


def parse_padding(value: Any) -> Tuple[float, float, float, float]:
    """返回 (top, right, bottom, left)。支持数值 / [v,h] / [t,h,b] / [t,r,b,l]。"""
    if value is None:
        return 0.0, 0.0, 0.0, 0.0
    if isinstance(value, (int, float)):
        v = float(value)
        return v, v, v, v
    if isinstance(value, (list, tuple)):
        nums = [resolve_length(x, 0.0, 0.0) or 0.0 for x in value]
        if len(nums) == 1:
            return nums[0], nums[0], nums[0], nums[0]
        if len(nums) == 2:
            return nums[0], nums[1], nums[0], nums[1]
        if len(nums) == 3:
            return nums[0], nums[1], nums[2], nums[1]
        if len(nums) >= 4:
            return nums[0], nums[1], nums[2], nums[3]
    return 0.0, 0.0, 0.0, 0.0


def side(
    style: Dict[str, Any],
    name: str,
    default: float = 0.0,
    fallback: Optional[str] = None,
) -> float:
    """读取 paddingTop / marginLeft 等单边值，未设置时回退到 padding / margin。"""
    if f"{name}" in style:
        return resolve_length(style[f"{name}"], 0.0, default) or default
    if name.endswith(("Top", "Right", "Bottom", "Left")) and fallback:
        base = style.get(fallback)
        if base is not None:
            t, r, b, left = parse_padding(base)
            return {
                "paddingTop": t, "paddingRight": r, "paddingBottom": b, "paddingLeft": left,
                "marginTop": t, "marginRight": r, "marginBottom": b, "marginLeft": left,
            }.get(name, default)
    return default


@dataclass
class Border:
    width: float = 0.0
    color: Color = None  # type: ignore[assignment]
    style: str = "solid"

    def __post_init__(self) -> None:
        if self.color is None:
            self.color = Color(hex="000000")

    def visible(self) -> bool:
        return self.width > 0 and self.color.is_visible()


def parse_border(value: Any) -> Optional[Border]:
    """解析 '2px solid #333' / {width, color, style}。"""
    if value is None:
        return None
    if isinstance(value, dict):
        w = resolve_length(value.get("width"), 0.0, 0.0) or 0.0
        color = parse_color(value.get("color"))
        return Border(width=w, color=color, style=str(value.get("style", "solid")))
    text = str(value).strip()
    m = re.match(r"^\s*([\d.]+)\s*px\s+([a-zA-Z]+)?\s*(.*)$", text)
    if m:
        return Border(width=float(m.group(1)), color=parse_color(m.group(3) or "#000"), style=(m.group(2) or "solid"))
    return None


def num(value: Any, default: float = 0.0) -> float:
    """从 style 值取数值。"""
    n, _ = parse_length(value)
    return default if n is None else n
