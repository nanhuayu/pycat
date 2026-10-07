"""`slidep lint` 语义：DSL 校验 + 高度溢出检测。

对齐 SlideDSL 强制规范：
- Slide 高度固定 720px，内容绝对不允许溢出
- padding 即安全区，内容区可用高度 = 720 - 上padding - 下padding
- Box 仅支持 display:flex；禁止 grid 与 calc()
- 一个 .slide 文件一个 <Slide>；文件最后必须是 <Slide>；禁止 import/export/module
- Text 换行必须用 <br/>，不允许 \\n
- FAIcon 必须设置 fill，否则不可见
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import CANVAS_H, CANVAS_W
from .dsl import Element, parse_document
from .layout import layout_slide


@dataclass
class Issue:
    level: str          # error | warning
    code: str
    message: str
    line: int = 0
    path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message[:1000],
            "line": self.line,
            "path": self.path,
        }


@dataclass
class LintResult:
    path: str = ""
    errors: List[Issue] = field(default_factory=list)
    warnings: List[Issue] = field(default_factory=list)
    content_height: float = 0.0
    available_height: float = 0.0
    overflow: float = 0.0
    nodes: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "ok": self.ok,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "errors": [e.to_dict() for e in self.errors[:20]],
            "errors_truncated": len(self.errors) > 20,
            "warnings": [w.to_dict() for w in self.warnings[:20]],
            "warnings_truncated": len(self.warnings) > 20,
            "metrics": {
                "content_height": round(self.content_height, 1),
                "available_height": round(self.available_height, 1),
                "overflow": round(self.overflow, 1),
                "nodes": self.nodes,
            },
        }


_GRID_PROP_RE = re.compile(r"grid[A-Z]\w*")


class Linter:
    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.base_dir = Path(base_dir) if base_dir else None

    def lint_text(self, text: str, path: str = "<string>") -> LintResult:
        result = LintResult(path=path)
        parsed = parse_document(text)

        for message in parsed.errors:
            result.errors.append(Issue("error", "dsl-parse", message, self._line_of(message)))
        for message in parsed.warnings:
            result.warnings.append(Issue("warning", "dsl-warning", message, self._line_of(message)))

        if re.search(r"\\n", text) and "<Text" in text:
            result.warnings.append(
                Issue("warning", "text-newline", "Text 换行应使用 <br /> 而非 \\\\n")
            )

        root = parsed.slides[0] if parsed.slides else parsed.root
        if root is None:
            result.errors.append(Issue("error", "missing-slide", "Expected exactly one Slide.", path=path))
            return result
        if len(parsed.slides) != 1 or root.tag.lower() != "slide":
            result.errors.append(Issue("error", "slide-count", "Expected exactly one Slide per source file.", path=path))

        self._walk(root, result)

        layout = layout_slide(root, self.base_dir)
        pending = [layout.slide]
        while pending:
            box = pending.pop()
            if box.x < -0.5 or box.y < -0.5 or box.x + box.w > CANVAS_W + 0.5 or box.y + box.h > CANVAS_H + 0.5:
                result.errors.append(Issue("error", "canvas-bounds", f"{box.el.tag} extends outside the canvas.", box.el.line, path))
            pending.extend(box.children)
        result.content_height = layout.content_height
        result.available_height = layout.available_height
        result.overflow = layout.overflow
        for message in layout.warnings:
            result.warnings.append(Issue("warning", "layout", message))

        if layout.overflow > 0.5:
            result.errors.append(
                Issue(
                    "error",
                    "overflow",
                    f"内容溢出安全区 {layout.overflow:.1f}px"
                    f"（内容 {layout.content_height:.1f}px > 可用 {layout.available_height:.1f}px，画布 {CANVAS_H:g}px）",
                    root.line,
                    path,
                )
            )
        elif layout.content_height > layout.available_height * 0.98:
            result.warnings.append(
                Issue(
                    "warning",
                    "near-overflow",
                    f"内容接近安全区上限（{layout.content_height:.1f}px / {layout.available_height:.1f}px）",
                )
            )

        ratio = layout.content_height / CANVAS_H if CANVAS_H else 0.0
        if ratio < 0.3:
            result.warnings.append(
                Issue("warning", "sparse", f"内容占比仅 {ratio:.0%}，规范建议 30%-85%")
            )
        elif ratio > 0.85:
            result.warnings.append(
                Issue("warning", "dense", f"内容占比达 {ratio:.0%}，规范建议 30%-85%")
            )
        return result

    def lint_file(self, path: Path) -> LintResult:
        path = Path(path)
        result = self.lint_text(path.read_text(encoding="utf-8", errors="replace"), str(path))
        return result

    def lint_project(self, slides_dir: Path) -> List[LintResult]:
        slides_dir = Path(slides_dir)
        results = []
        for file in sorted(slides_dir.glob("*.slide")):
            results.append(self.lint_file(file))
        return results

    # -- 内部 --------------------------------------------------------------
    def _walk(self, el: Element, result: LintResult) -> None:
        result.nodes += 1
        style = el.style
        tag = el.tag.lower()
        supported = {"slide", "box", "text", "span", "br", "image", "table", "chart", "faicon", "qrcode", "codeblock", "svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon"}
        if tag not in supported:
            result.errors.append(Issue("error", "unsupported-component", f"Unsupported component: {el.tag}", el.line, result.path))

        # Validate declarations, not literal slide text or displayed code.
        if any("calc(" in str(value).lower() for value in style.values()):
            result.errors.append(Issue("error", "css-calc", "CSS calc() is unsupported; use explicit dimensions.", el.line, result.path))
        if str(style.get("display", "")).lower() in {"grid", "table"} or any(_GRID_PROP_RE.match(str(key)) for key in style):
            result.errors.append(Issue("error", "css-grid", "Use flex layout; grid declarations are unsupported.", el.line, result.path))

        if tag == "faicon":
            fill = el.props.get("fill") or style.get("fill") or style.get("color")
            if not fill:
                result.errors.append(
                    Issue("error", "faicon-fill", "FAIcon 必须设置 fill，否则完全不可见", el.line, result.path)
                )
            name = str(el.props.get("name") or "")
            if not name:
                result.errors.append(Issue("error", "faicon-name", "FAIcon 缺少 name", el.line, result.path))

        if tag == "qrcode" and not el.props.get("text"):
            result.errors.append(Issue("error", "qrcode-text", "QRCode 缺少 text", el.line, result.path))

        if tag in ("table",) and not el.props.get("cells"):
            result.errors.append(Issue("error", "table-cells", "Table 缺少 cells", el.line, result.path))

        if tag == "chart":
            chart_type = str(el.props.get("chartType") or el.props.get("type") or "barChart").lower()
            supported_charts = {"linechart", "line", "barchart", "bar", "column", "column-stacked", "bar-horizontal", "piechart", "pie", "doughnutchart", "doughnut", "areachart", "area"}
            if chart_type not in supported_charts:
                result.errors.append(Issue("error", "chart-type", f"Unsupported chart type: {chart_type}", el.line, result.path))
            series = el.props.get("series") or el.props.get("data")
            if not series:
                result.errors.append(Issue("error", "chart-series", "Chart 缺少 series/data", el.line, result.path))

        if tag == "image" and not el.props.get("src"):
            result.errors.append(Issue("error", "image-src", "Image 缺少 src", el.line, result.path))
        elif tag == "image" and self.base_dir:
            src = str(el.props.get("src") or "")
            if src.startswith(("http://", "https://")):
                result.errors.append(Issue("error", "remote-image", "Save remote images to local assets before building.", el.line, result.path))
            elif src:
                candidate = (self.base_dir / src).resolve()
                try:
                    candidate.relative_to(self.base_dir.resolve())
                except ValueError:
                    result.errors.append(Issue("error", "image-scope", f"Image escapes project: {src}", el.line, result.path))
                if not candidate.is_file():
                    result.errors.append(
                        Issue("error", "image-missing", f"找不到图片资源：{src}", el.line, result.path)
                    )

        if str(style.get("position", "")).lower() == "absolute" and tag == "slide":
            result.warnings.append(
                Issue("warning", "slide-absolute", "Slide 不应使用绝对定位绕过安全区", el.line, result.path)
            )

        if style.get("padding") is None and tag == "slide":
            result.warnings.append(
                Issue("warning", "slide-padding", "Slide 未设置 padding（安全区），内容可能贴边", el.line, result.path)
            )

        for child in el.children:
            if isinstance(child, Element):
                self._walk(child, result)

    @staticmethod
    def _line_of(message: str) -> int:
        m = re.search(r"line (\d+)", message)
        return int(m.group(1)) if m else 0


def lint(text: str, path: str = "<string>", base_dir: Optional[Path] = None) -> LintResult:
    return Linter(base_dir).lint_text(text, path)
