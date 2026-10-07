"""Flex 布局引擎：SlideDSL → 绝对像素坐标。

采用「先测量（intrinsic）后放置（place）」的两阶段算法：

- Slide：固定 1280x720（16:9），padding 即安全区
- Box：仅 flex（默认 column）；支持 gap / justifyContent / alignItems / flex /
  百分比 / padding / margin / position:absolute
- 盒模型为 border-box（设置 width 已包含 padding）
- 禁用 grid 与 calc()（由 lint 负责报错）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import CANVAS_H, CANVAS_W
from .dsl import Break, Element, TextNode
from .style import parse_padding, resolve_length

_LATIN_RATIO = 0.55
_CJK_RE = re.compile(
    r"[\u1100-\u115F\u2E80-\uA4CF\uAC00-\uD7A3\uF900-\uFAFF\uFE30-\uFE4F\uFF00-\uFF60\uFFE0-\uFFE6]"
)
_ICON_DEFAULT = 24.0
_TOKEN_RE = re.compile(r"[^\s]+|\s+|\n")
_FLEX_NUM_RE = re.compile(r"^\s*(-?[\d.]+)")
_WRAP_EPS = 0.5   # 折行容差（px），避免浮点误差导致"刚好等宽"误换行


# --------------------------------------------------------------------------
# 文本度量
# --------------------------------------------------------------------------

def text_units(text: str) -> float:
    """返回文本宽度（em 数）：CJK/全角按 1em，其余按比例。"""
    units = 0.0
    for ch in text:
        if ch == "\n":
            continue
        units += 1.0 if _CJK_RE.match(ch) else _LATIN_RATIO
    return units


def runs_of(el: Element) -> List[Tuple[str, Dict[str, Any]]]:
    """把 Text 子树展平为 (文本, 样式) 序列；Break / <br/> 记为 '\\n'。"""
    out: List[Tuple[str, Dict[str, Any]]] = []

    def walk(nodes: Sequence[Any], style: Dict[str, Any]) -> None:
        for node in nodes:
            if isinstance(node, TextNode):
                out.append((node.text, style))
            elif isinstance(node, Break):
                out.append(("\n", style))
            elif isinstance(node, Element):
                if node.tag.lower() == "br":
                    out.append(("\n", style))
                    continue
                child_style = dict(style)
                child_style.update(node.style)
                walk(node.children, child_style)

    walk(el.children, {})
    return out


def _wrap_tokens(text: str) -> List[str]:
    return ["\n" if piece == "\n" else piece for piece in _TOKEN_RE.findall(text)]


def text_natural_units(runs: Sequence[Tuple[str, Dict[str, Any]]], base_size: float) -> float:
    """自然宽度（px）：与 measure_text 使用同一套度量规则（含加粗系数）。"""
    total = 0.0
    for text, style in runs:
        size = float(style.get("fontSize") or base_size) or base_size
        weight = str(style.get("fontWeight", "")).lower()
        bold = weight in ("bold", "bolder", "600", "700", "800", "900") or style.get("bold") is True
        total += text_units(text) * (size / base_size) * (1.05 if bold else 1.0)
    return total * base_size


def measure_text(
    runs: Sequence[Tuple[str, Dict[str, Any]]],
    width: float,
    base_style: Dict[str, Any],
) -> Tuple[float, List[List[Tuple[str, Dict[str, Any]]]]]:
    """按宽度折行，返回 (总高度, 行列表)。"""
    font_size = float(base_style.get("fontSize") or 16) or 16.0
    line_height = float(base_style.get("lineHeight") or 1.4) or 1.4
    line_h = font_size * line_height
    if width <= 4:
        width = 4
    limit = width / font_size

    lines: List[List[Tuple[str, Dict[str, Any]]]] = []
    current: List[Tuple[str, Dict[str, Any]]] = []
    current_units = 0.0

    def flush() -> None:
        nonlocal current, current_units
        lines.append(current)
        current = []
        current_units = 0.0

    for text, style in runs:
        size = float(style.get("fontSize") or font_size) or font_size
        weight = str(style.get("fontWeight", "")).lower()
        bold = weight in ("bold", "bolder", "600", "700", "800", "900") or style.get("bold") is True
        ratio = (size / font_size) * (1.05 if bold else 1.0)
        if text == "\n":
            flush()
            continue
        for token in _wrap_tokens(text):
            if token == "\n":
                flush()
                continue
            token_units = text_units(token) * ratio
            if current_units + token_units > limit + _WRAP_EPS and current:
                flush()
            # 超长片段硬切
            guard = 0
            while token_units > limit + _WRAP_EPS and token and guard < 512:
                guard += 1
                cut = max(1, int(len(token) * limit / token_units))
                piece, token = token[:cut], token[cut:]
                current.append((piece, style))
                flush()
                token_units = text_units(token) * ratio
            if token:
                current.append((token, style))
                current_units += token_units
    if current or not lines:
        flush()
    return max(1, len(lines)) * line_h, lines


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------

@dataclass
class Box:
    el: Element
    kind: str
    style: Dict[str, Any] = field(default_factory=dict)
    padding: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    content_x: float = 0.0
    content_y: float = 0.0
    content_w: float = 0.0
    content_h: float = 0.0
    children: List["Box"] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)
    absolute: bool = False
    lines: List[Any] = field(default_factory=list)

    def flow_children(self) -> List["Box"]:
        return [c for c in self.children if not c.absolute]

    def absolute_children(self) -> List["Box"]:
        return [c for c in self.children if c.absolute]


@dataclass
class LayoutResult:
    slide: Box
    warnings: List[str] = field(default_factory=list)
    content_height: float = 0.0
    available_height: float = 0.0
    overflow: float = 0.0


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

def _gap_of(style: Dict[str, Any]) -> float:
    value = style.get("gap", style.get("rowGap", style.get("columnGap")))
    return max(0.0, float(resolve_length(value, 0.0, 0.0) or 0.0))


def _flex_of(style: Dict[str, Any]) -> float:
    """解析 flex 权重：1 / '1' / '1 1 auto' / true。"""
    value = style.get("flex")
    if value is None or value is False:
        return 0.0
    if value is True:
        return 1.0
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    text = str(value).strip().lower()
    if text in ("", "none", "0", "0 0 auto", "initial", "auto"):
        return 0.0
    m = _FLEX_NUM_RE.match(text)
    return max(0.0, float(m.group(1))) if m else 0.0


def _kind_of(tag: str) -> str:
    return {
        "slide": "box",
        "box": "box",
        "text": "text",
        "span": "text",
        "image": "image",
        "picture": "image",
        "table": "table",
        "chart": "chart",
        "faicon": "icon",
        "qrcode": "qrcode",
        "codeblock": "code",
        "svg": "svg",
        "diagram": "diagram",
    }.get(tag.lower(), "box")


# --------------------------------------------------------------------------
# 布局器
# --------------------------------------------------------------------------

class Layouter:
    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.base_dir = Path(base_dir) if base_dir else None
        self.warnings: List[str] = []

    # -- 入口 -------------------------------------------------------------
    def layout(self, root: Element) -> LayoutResult:
        slide = self._build(root, is_slide=True)
        self._place(slide, 0.0, 0.0, CANVAS_W, CANVAS_H)
        content_height = 0.0
        for child in slide.children:
            if child.absolute:
                continue
            content_height = max(content_height, child.y + child.h - slide.content_y)
        available = slide.content_h
        return LayoutResult(
            slide=slide,
            warnings=list(self.warnings),
            content_height=max(0.0, content_height),
            available_height=available,
            overflow=max(0.0, content_height - available),
        )

    # -- 构建 Box 树 -------------------------------------------------------
    def _build(self, el: Element, is_slide: bool = False) -> Box:
        style = el.style if isinstance(el.style, dict) else {}
        kind = "slide" if is_slide else _kind_of(el.tag)
        box = Box(
            el=el,
            kind=kind,
            style=style,
            padding=parse_padding(style.get("padding")),
            absolute=str(style.get("position", "")).lower() == "absolute" and not is_slide,
        )
        if kind == "box" or is_slide:
            box.children = self._build_children(el)
        return box

    def _build_children(self, el: Element) -> List[Box]:
        out: List[Box] = []
        pending_text: List[TextNode] = []

        def flush_text() -> None:
            nonlocal pending_text
            if pending_text:
                synthetic = Element(tag="Text", props={"style": {}}, children=list(pending_text))
                out.append(self._build(synthetic))
                pending_text = []

        for child in el.children:
            if isinstance(child, Element):
                if child.tag.lower() == "animation":  # 透明包装
                    flush_text()
                    out.extend(self._build_children(child))
                    continue
                flush_text()
                out.append(self._build(child))
            elif isinstance(child, TextNode):
                if child.text.strip():
                    pending_text.append(child)
        flush_text()
        return out

    # -- 测量 -------------------------------------------------------------
    def _intrinsic(
        self,
        box: Box,
        avail_w: float,
        avail_h: float,
        fill_width: bool = False,
    ) -> Tuple[float, float]:
        """返回 (宽, 高)。fill_width=True 表示该元素在父容器中会被拉伸到可用宽度
        （CSS 交叉轴 stretch），此时文本按可用宽度而非自然宽度折行。"""
        style = box.style
        kind = box.kind
        pad = box.padding
        ew = resolve_length(style.get("width"), avail_w, None)
        eh = resolve_length(style.get("height"), avail_h, None)

        if kind == "text":
            return self._measure_text_box(box, ew, eh, avail_w, fill_width)
        if kind == "icon":
            size = resolve_length(style.get("width"), 0.0, None) or _ICON_DEFAULT
            height = resolve_length(style.get("height"), 0.0, None) or size
            return (ew or size, eh or height)
        if kind == "image":
            return self._measure_image_box(box, ew, eh, avail_w)
        if kind == "qrcode":
            size = float(box.el.props.get("width") or 160)
            return (ew or size, eh or size)
        if kind == "table":
            rows = box.el.props.get("cells") or []
            count = len(rows) if isinstance(rows, list) else 0
            font = float((box.el.props.get("defaultTextStyle") or {}).get("fontSize") or 16)
            height = eh if eh is not None else max(60.0, max(28.0, font * 1.9) * max(1, count) + 2)
            width = ew if ew is not None else (avail_w if avail_w > 0 else 480.0)
            return (width, height)
        if kind == "chart":
            width = ew if ew is not None else (avail_w if avail_w > 0 else 480.0)
            height = eh if eh is not None else 320.0
            return (width, height)
        if kind == "code":
            code = str(box.el.props.get("code") or "")
            lines = max(1, len(code.splitlines()))
            font = float(box.el.props.get("fontSize") or 12)
            header = 26.0 if box.el.props.get("macHeader", True) else 0.0
            width = ew if ew is not None else (avail_w if avail_w > 0 else 480.0)
            height = eh if eh is not None else header + lines * font * 1.5 + 24.0
            return (width, height)
        if kind == "svg":
            width = ew if ew is not None else float(box.el.props.get("width") or 100)
            height = eh if eh is not None else float(box.el.props.get("height") or 100)
            return (width, height)
        if kind == "diagram":
            width = ew if ew is not None else (avail_w if avail_w > 0 else 480.0)
            height = eh if eh is not None else float(box.el.props.get("height") or 320)
            return (width, height)

        # 容器
        axis = "row" if str(style.get("flexDirection", "column")).lower() == "row" else "column"
        gap = _gap_of(style)
        flow = [c for c in box.children if not c.absolute]
        inner_w = max(0.0, (ew if ew is not None else avail_w) - pad[1] - pad[3])
        inner_h = max(0.0, (eh if eh is not None else avail_h) - pad[0] - pad[2])

        main_total = 0.0
        cross_max = 0.0
        for child in flow:
            cw, ch = self._intrinsic(child, inner_w, inner_h)
            main, cross = (cw, ch) if axis == "row" else (ch, cw)
            main_total += main
            cross_max = max(cross_max, cross)
        main_total += gap * max(0, len(flow) - 1)

        if axis == "row":
            width = ew if ew is not None else main_total + pad[1] + pad[3]
            height = eh if eh is not None else cross_max + pad[0] + pad[2]
        else:
            width = ew if ew is not None else cross_max + pad[1] + pad[3]
            height = eh if eh is not None else main_total + pad[0] + pad[2]
        return (max(0.0, width), max(0.0, height))

    def _measure_text_box(
        self,
        box: Box,
        ew: Optional[float],
        eh: Optional[float],
        avail_w: float,
        fill_width: bool = False,
    ) -> Tuple[float, float]:
        runs = runs_of(box.el)
        if not runs:
            return (ew or 0.0, eh or 0.0)
        style = box.style
        base_size = float(style.get("fontSize") or 16) or 16.0
        natural = text_natural_units(runs, base_size)
        nowrap = str(style.get("whiteSpace", "")).lower() == "nowrap"
        if ew is not None:
            width = ew
        elif fill_width and avail_w > 0:
            width = avail_w          # 被拉伸：按可用宽度折行
        elif nowrap or avail_w <= 0:
            width = natural
        else:
            width = min(avail_w, natural) if natural > 0 else avail_w
        height, lines = measure_text(runs, max(8.0, width), style)
        box.lines = lines
        return (width, eh if eh is not None else height)

    def _measure_image_box(
        self,
        box: Box,
        ew: Optional[float],
        eh: Optional[float],
        avail_w: float,
    ) -> Tuple[float, float]:
        natural = self._image_natural(box)
        if not natural:
            return (ew or 240.0, eh or 160.0)
        nw, nh = natural
        if ew is not None:
            width = ew
        elif avail_w > 0:
            width = min(avail_w, nw)
        else:
            width = nw
        if eh is not None:
            height = eh
        else:
            height = width * (nh / nw) if nw else nh
        return (width, height)

    def _image_natural(self, box: Box) -> Optional[Tuple[float, float]]:
        src = box.el.props.get("src")
        if not src or not self.base_dir:
            return None
        path = (self.base_dir / str(src)).resolve()
        if not path.exists():
            return None
        try:
            from PIL import Image

            with Image.open(path) as img:
                return float(img.width), float(img.height)
        except Exception:
            return None

    # -- 放置 -------------------------------------------------------------
    def _place(self, box: Box, x: float, y: float, w: float, h: float) -> None:
        box.x, box.y, box.w, box.h = x, y, max(0.0, w), max(0.0, h)
        pad = box.padding
        box.content_x = box.x + pad[3]
        box.content_y = box.y + pad[0]
        box.content_w = max(0.0, box.w - pad[1] - pad[3])
        box.content_h = max(0.0, box.h - pad[0] - pad[2])
        if box.children:
            self._place_children(box)

    def _place_children(self, box: Box) -> None:
        style = box.style
        axis = "row" if str(style.get("flexDirection", "column")).lower() == "row" else "column"
        align = str(style.get("alignItems", "stretch")).lower()
        justify = str(style.get("justifyContent", "flex-start")).lower()
        gap = _gap_of(style)
        flow = [c for c in box.children if not c.absolute]
        cw, ch = box.content_w, box.content_h
        main_avail = cw if axis == "row" else ch
        cross_avail = ch if axis == "row" else cw
        total_gap = gap * max(0, len(flow) - 1)

        # 1) 主轴尺寸
        mains: List[float] = []
        flex_items: List[Tuple[int, float]] = []
        for child in flow:
            explicit = resolve_length(
                child.style.get("width" if axis == "row" else "height"), main_avail, None
            )
            weight = _flex_of(child.style)
            if explicit is not None:
                mains.append(explicit)
            elif weight > 0:
                mains.append(-1.0)  # 占位，稍后分配
                flex_items.append((len(mains) - 1, weight))
            else:
                iw, ih = self._intrinsic(child, cw, ch)
                mains.append(iw if axis == "row" else ih)

        if flex_items:
            used = sum(m for m in mains if m >= 0)
            remaining = main_avail - total_gap - used
            weight_sum = sum(weight for _, weight in flex_items) or 1.0
            for index, weight in flex_items:
                if remaining > 0:
                    mains[index] = remaining * weight / weight_sum
                else:  # 主轴未定（auto 高度）→ 回退为内容尺寸
                    iw, ih = self._intrinsic(flow[index], cw, ch)
                    mains[index] = iw if axis == "row" else ih

        # 2) 交叉轴尺寸
        crosses: List[float] = []
        for index, child in enumerate(flow):
            explicit = resolve_length(
                child.style.get("height" if axis == "row" else "width"), cross_avail, None
            )
            if explicit is not None:
                crosses.append(explicit)
                continue
            # 图片保持原始宽高比：未显式指定的那一边按比例推导，避免拉伸变形
            if child.kind == "image":
                natural = self._image_natural(child)
                if natural and natural[0] and natural[1]:
                    nw, nh = natural
                    ratio = (nh / nw) if axis == "row" else (nw / nh)
                    crosses.append(max(0.0, mains[index] * ratio))
                    continue
            # 图标默认正方形，不随交叉轴拉伸
            if child.kind == "icon":
                size = (
                    resolve_length(child.style.get("width"), 0.0, None)
                    or resolve_length(child.style.get("height"), 0.0, None)
                    or _ICON_DEFAULT
                )
                crosses.append(size)
                continue
            if align == "stretch" and cross_avail > 0:
                crosses.append(cross_avail)
            else:
                iw, ih = self._intrinsic(child, cw, ch)
                crosses.append(ih if axis == "row" else iw)

        # 3) justifyContent 分布
        used_main = sum(mains) + total_gap
        extra = max(0.0, main_avail - used_main)
        offset = 0.0
        between = gap
        if justify == "center":
            offset = extra / 2.0
        elif justify in ("flex-end", "end"):
            offset = extra
        elif justify == "space-between" and len(flow) > 1:
            between = gap + extra / (len(flow) - 1)
        elif justify == "space-around" and flow:
            unit = extra / len(flow)
            offset = unit / 2.0
            between = gap + unit
        elif justify == "space-evenly" and flow:
            unit = extra / (len(flow) + 1)
            offset = unit
            between = gap + unit

        if axis == "row":
            pass  # 交叉轴偏移在下方逐个计算

        # 4) 逐个放置
        cursor = offset
        for index, child in enumerate(flow):
            main = max(0.0, mains[index])
            cross = max(0.0, crosses[index])
            same = self._cross_offset(align, cross, cross_avail)
            if axis == "row":
                self._place(child, box.content_x + cursor, box.content_y + same, main, cross)
            else:
                self._place(child, box.content_x + same, box.content_y + cursor, cross, main)
            cursor += main + between

        # 5) 绝对定位子元素
        for child in box.children:
            if not child.absolute:
                continue
            self._place_absolute(box, child)

    @staticmethod
    def _cross_offset(align: str, size: float, available: float) -> float:
        if align == "center":
            return max(0.0, (available - size) / 2.0)
        if align in ("flex-end", "end"):
            return max(0.0, available - size)
        return 0.0

    def _place_absolute(self, parent: Box, child: Box) -> None:
        style = child.style
        w = resolve_length(style.get("width"), parent.w, None)
        h = resolve_length(style.get("height"), parent.h, None)
        if w is None or h is None:
            iw, ih = self._intrinsic(child, parent.w, parent.h)
            w = iw if w is None else w
            h = ih if h is None else h
        x = parent.x
        y = parent.y
        if style.get("left") is not None:
            x = parent.x + (resolve_length(style.get("left"), parent.w, 0.0) or 0.0)
        elif style.get("right") is not None:
            right = resolve_length(style.get("right"), parent.w, 0.0) or 0.0
            x = parent.x + parent.w - w - right
        if style.get("top") is not None:
            y = parent.y + (resolve_length(style.get("top"), parent.h, 0.0) or 0.0)
        elif style.get("bottom") is not None:
            bottom = resolve_length(style.get("bottom"), parent.h, 0.0) or 0.0
            y = parent.y + parent.h - h - bottom
        self._place(child, x, y, w, h)


def layout_slide(root: Element, base_dir: Optional[Path] = None) -> LayoutResult:
    return Layouter(base_dir).layout(root)
