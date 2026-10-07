"""SlideDSL 解析器（JSX 风格）→ Element 树。

对齐 WorkBuddy `tencent-pptx` 规范：
- 每页一个 `.slide` 文件；文件最后必须是一个 `<Slide>`；不允许 import/export/module
- 支持组件：Slide / Box / Text / Image / Picture / Table / Chart / FAIcon / SVG(svg) /
  QRCode / CodeBlock / Diagram / Animation
- Text 内支持 `<span style={{...}}>` 富文本与 `<br />`；换行禁止用 \\n
- style 使用 JS 对象字面量（camelCase）；支持数组、嵌套对象、模板字面量
- 有限支持 `{items.map((item, idx) => (...))}` 的字面量数组展开
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------


@dataclass
class TextNode:
    text: str
    style: Dict[str, Any] = field(default_factory=dict)
    line: int = 0


@dataclass
class Break:
    line: int = 0


@dataclass
class Element:
    tag: str
    props: Dict[str, Any] = field(default_factory=dict)
    children: List[Any] = field(default_factory=list)
    line: int = 0
    self_closing: bool = False

    @property
    def style(self) -> Dict[str, Any]:
        style = self.props.get("style")
        return style if isinstance(style, dict) else {}

    def find_all(self, tag: str) -> List["Element"]:
        out: List["Element"] = []
        for child in self.children:
            if isinstance(child, Element):
                if child.tag.lower() == tag.lower():
                    out.append(child)
                out.extend(child.find_all(tag))
        return out


@dataclass
class ParseResult:
    root: Optional[Element]
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    slides: List[Element] = field(default_factory=list)


# --------------------------------------------------------------------------
# 平衡扫描（处理引号 / 模板字面量 / 嵌套括号）
# --------------------------------------------------------------------------

_OPEN = {"{": "}", "(": ")", "[": "]"}


def _scan_balanced(text: str, start: int) -> Tuple[str, int]:
    """从 text[start] 的 { ( [ 开始，返回 (内部内容, 结束位置后一位)。"""
    opener = text[start]
    depth = 0
    i = start
    in_str: Optional[str] = None
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in "'\"`":
            in_str = ch
            i += 1
            continue
        if ch == "/" and i + 1 < len(text) and text[i + 1] == "/":
            nl = text.find("\n", i)
            i = len(text) if nl == -1 else nl + 1
            continue
        if ch == "/" and i + 1 < len(text) and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = len(text) if end == -1 else end + 2
            continue
        if ch in _OPEN:
            depth += 1
        elif ch in "})]":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i], i + 1
        i += 1
    raise SyntaxError(f"unbalanced '{opener}' at {start}")


# --------------------------------------------------------------------------
# JS 字面量求值（受限：对象/数组/字符串/数字/布尔/null/模板字面量）
# --------------------------------------------------------------------------

_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$")


class RawExpr(str):
    """无法静态求值的表达式。"""


def parse_js_value(text: str, scope: Optional[Dict[str, Any]] = None) -> Any:
    scope = scope or {}
    s = text.strip()
    if not s:
        return ""
    # 模板字面量
    if s.startswith("`") and s.endswith("`") and len(s) >= 2:
        body = s[1:-1]
        return _interpolate(body, scope)
    # 普通字符串
    if len(s) >= 2 and s[0] in "'\"" and s[-1] == s[0]:
        return _unescape(s[1:-1])
    # 对象
    if s.startswith("{") and s.endswith("}"):
        try:
            inner, end = _scan_balanced(s, 0)
            if end != len(s):
                return RawExpr(s)
        except SyntaxError:
            return RawExpr(s)
        return _parse_object(inner, scope)
    # 数组
    if s.startswith("[") and s.endswith("]"):
        try:
            inner, end = _scan_balanced(s, 0)
            if end != len(s):
                return RawExpr(s)
        except SyntaxError:
            return RawExpr(s)
        return [_parse_scalar(p, scope) for p in _split_top(inner)]
    if s in ("true", "false"):
        return s == "true"
    if s in ("null", "undefined", "None"):
        return None
    if _NUM_RE.match(s):
        return float(s) if ("." in s or "e" in s.lower()) else int(s)
    # 作用域内标识符
    if s in scope:
        return scope[s]
    return RawExpr(s)


def _unescape(text: str) -> str:
    return (
        text.replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace("\\r", "")
        .replace('\\"', '"')
        .replace("\\'", "'")
        .replace("\\\\", "\\")
    )


def _interpolate(body: str, scope: Dict[str, Any]) -> str:
    def repl(match: "re.Match[str]") -> str:
        expr = match.group(1).strip()
        value = _resolve_path(expr, scope)
        if value is _MISSING:
            value = parse_js_value(expr, scope)
        if isinstance(value, RawExpr):
            return match.group(0)
        return "" if value is None else str(value)

    return _unescape(re.sub(r"\$\{([^}]*)\}", repl, body))


_MISSING = object()


def _resolve_path(expr: str, scope: Dict[str, Any]) -> Any:
    """解析 item.label / idx 这类作用域路径。"""
    parts = [p for p in re.split(r"[.\[\]]", expr) if p and not p.isdigit()]
    if not parts:
        return _MISSING
    if parts[0] not in scope:
        return _MISSING
    value: Any = scope[parts[0]]
    for part in parts[1:]:
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return _MISSING
    return value


def _split_top(text: str) -> List[str]:
    """按顶层逗号切分。"""
    parts: List[str] = []
    depth = 0
    buf: List[str] = []
    in_str: Optional[str] = None
    i = 0
    while i < len(text):
        ch = text[i]
        if in_str:
            buf.append(ch)
            if ch == "\\":
                if i + 1 < len(text):
                    buf.append(text[i + 1])
                    i += 2
                    continue
            elif ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in "'\"`":
            in_str = ch
            buf.append(ch)
            i += 1
            continue
        if ch in _OPEN:
            depth += 1
        elif ch in "})]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    if buf:
        parts.append("".join(buf))
    return [p for p in (p.strip() for p in parts) if p != ""]


def _parse_scalar(text: str, scope: Dict[str, Any]) -> Any:
    s = text.strip()
    if not s:
        return ""
    if s.startswith("{") or s.startswith("["):
        return parse_js_value(s, scope)
    if len(s) >= 2 and s[0] in "'\"`" and s[-1] == s[0]:
        return parse_js_value(s, scope)
    if s in ("true", "false") or s in ("null", "undefined") or _NUM_RE.match(s):
        return parse_js_value(s, scope)
    return _resolve_path(s, scope) if s in scope else RawExpr(s)


def _parse_object(inner: str, scope: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for chunk in _split_top(inner):
        key, value = _split_key_value(chunk)
        if key is None:
            continue
        out[key] = _parse_scalar(value, scope) if value is not None else True
    return out


def _split_key_value(chunk: str) -> Tuple[Optional[str], Optional[str]]:
    depth = 0
    in_str: Optional[str] = None
    i = 0
    while i < len(chunk):
        ch = chunk[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in "'\"`":
            in_str = ch
        elif ch in _OPEN:
            depth += 1
        elif ch in "})]":
            depth -= 1
        elif ch == ":" and depth == 0:
            key = chunk[:i].strip()
            value = chunk[i + 1 :].strip()
            key = key.strip("'\"")
            # 简写方法 / 三目等复杂表达式：保留原样
            return key, value
        i += 1
    key = chunk.strip().strip("'\"")
    if re.match(r"^[A-Za-z_$][\w$]*$", key):
        return key, None
    return None, None


# --------------------------------------------------------------------------
# 解析器
# --------------------------------------------------------------------------

_TAG_START = re.compile(r"<([A-Za-z][\w.-]*)")
_ATTR_NAME = re.compile(r"[A-Za-z_:][\w:.-]*")


class DSLParser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.i = 0
        self.line = 1
        self.warnings: List[str] = []
        self.errors: List[str] = []

    # -- 入口 --------------------------------------------------------------
    def parse(self) -> List[Element]:
        roots: List[Element] = []
        while self.i < len(self.text):
            node = self._next_node(stop_tag=None, scope={})
            if node is None:
                break
            if isinstance(node, Element):
                roots.append(node)
            elif isinstance(node, TextNode) and node.text.strip():
                self.errors.append(f"line {node.line}: 顶层出现游离文本：{node.text.strip()[:40]!r}")
        return roots

    # -- 主循环 ------------------------------------------------------------
    def _next_node(self, stop_tag: Optional[str], scope: Dict[str, Any]) -> Optional[Any]:
        """返回 Element / TextNode / None(到达 stop_tag)。"""
        while self.i < len(self.text):
            ch = self.text[self.i]
            if ch == "<":
                # 结束标签？
                if self.text.startswith("</", self.i):
                    m = re.match(r"</\s*([A-Za-z][\w.-]*)\s*>", self.text[self.i :])
                    if not m:
                        self._advance(1)
                        continue
                    name = m.group(1)
                    if stop_tag and name.lower() == stop_tag.lower():
                        self._advance(m.end())
                        self._closed_at = (name.lower(), self.i)
                        return None
                    self.errors.append(f"line {self.line}: 意外的结束标签 </{name}>")
                    self._advance(m.end())
                    continue
                if self.text.startswith("<!--", self.i):
                    end = self.text.find("-->", self.i)
                    self._advance(len(self.text) - self.i if end == -1 else end + 3 - self.i)
                    continue
                m = _TAG_START.match(self.text, self.i)
                if not m:
                    self._advance(1)
                    continue
                return self._parse_element(scope)
            if ch == "{":
                node = self._parse_brace_child(scope)
                if node is None:
                    continue
                return node
            # 文本
            start = self.i
            while self.i < len(self.text) and self.text[self.i] not in "<{":
                self._advance(1)
            raw = self.text[start : self.i]
            if stop_tag is None and not raw.strip():
                continue
            node = self._make_text(raw, scope)
            if node is not None:
                return node
        return None

    def _make_text(self, raw: str, scope: Dict[str, Any]) -> Optional[Any]:
        if "\\n" in raw and "\n" not in raw:
            self.warnings.append(f"line {self.line}: Text 内出现 \\\\n，规范要求使用 <br />")
        text = _interpolate(raw, scope) if scope else raw
        text = text.replace("\r", "")
        if not text.strip() and "\n" not in text:
            return None
        return TextNode(text=text, line=self.line)

    # -- 元素 --------------------------------------------------------------
    def _parse_element(self, scope: Dict[str, Any]) -> Element:
        start_line = self.line
        m = _TAG_START.match(self.text, self.i)
        assert m is not None
        tag = m.group(1)
        # 注意：使用 pattern.match(text, pos) 时 m.end() 是绝对位置，需换算为长度
        self._advance(m.end() - self.i)
        props: Dict[str, Any] = {}
        self_closing = False
        while self.i < len(self.text):
            ch = self.text[self.i]
            if ch.isspace():
                self._advance(1)
                continue
            if self.text.startswith("/>", self.i):
                self_closing = True
                self._advance(2)
                break
            if ch == ">":
                self._advance(1)
                break
            if ch == "{":
                try:
                    inner, end = _scan_balanced(self.text, self.i)
                except SyntaxError as exc:
                    self.errors.append(f"line {self.line}: {exc}")
                    self._advance(1)
                    continue
                self._advance(end - self.i)
                m2 = re.match(r"^\s*\.\.\.\s*([\s\S]+)$", inner)
                if m2:
                    spread = parse_js_value(m2.group(1), scope)
                    if isinstance(spread, dict):
                        props.update(spread)
                else:
                    self.warnings.append(f"line {self.line}: 忽略展开表达式 {{{inner.strip()[:30]}}}")
                continue
            am = _ATTR_NAME.match(self.text, self.i)
            if not am:
                self._advance(1)
                continue
            name = am.group(0)
            self._advance(am.end() - self.i)
            while self.i < len(self.text) and self.text[self.i].isspace():
                self._advance(1)
            value: Any = True
            if self.i < len(self.text) and self.text[self.i] == "=":
                self._advance(1)
                while self.i < len(self.text) and self.text[self.i].isspace():
                    self._advance(1)
                value = self._parse_attr_value(scope)
            props[name] = value

        el = Element(tag=tag, props=props, line=start_line, self_closing=self_closing)
        if self_closing:
            return el

        closed = False
        children: List[Any] = []
        while self.i < len(self.text):
            node = self._next_node(stop_tag=tag, scope=scope)
            if node is None:
                closed = getattr(self, "_closed_at", None) == (tag.lower(), self.i)
                break
            if isinstance(node, list):
                children.extend(node)
            else:
                children.append(node)
        if not closed:
            self.errors.append(f"line {start_line}: Unclosed tag <{tag}>")
        el.children = children
        return el

    def _parse_attr_value(self, scope: Dict[str, Any]) -> Any:
        ch = self.text[self.i]
        if ch in "'\"":
            end = self.i + 1
            buf: List[str] = []
            while end < len(self.text):
                c = self.text[end]
                if c == "\\":
                    buf.append(self.text[end : end + 2])
                    end += 2
                    continue
                if c == ch:
                    break
                buf.append(c)
                end += 1
            inner = "".join(buf)
            self._advance(end + 1 - self.i)
            return _unescape(inner)
        if ch == "{":
            try:
                inner, end = _scan_balanced(self.text, self.i)
            except SyntaxError as exc:
                self.errors.append(f"line {self.line}: {exc}")
                self._advance(1)
                return None
            self._advance(end - self.i)
            return parse_js_value(inner, scope)
        m = re.match(r"[^\s/>]+", self.text[self.i :])
        if not m:
            return True
        self._advance(m.end())
        return m.group(0)

    # -- 花括号子节点 ------------------------------------------------------
    def _parse_brace_child(self, scope: Dict[str, Any]) -> Optional[Any]:
        try:
            inner, end = _scan_balanced(self.text, self.i)
        except SyntaxError as exc:
            self.errors.append(f"line {self.line}: {exc}")
            self._advance(1)
            return None
        self._advance(end - self.i)
        stripped = inner.strip()
        if not stripped or stripped.startswith("//"):
            return None
        if stripped.startswith("/*"):
            return None
        value = parse_js_value(stripped, scope)
        if isinstance(value, RawExpr):
            if scope and stripped in scope:
                value = scope[stripped]
            else:
                self.errors.append(f"line {self.line}: unsupported expression {{{stripped[:40]}}}; use a literal")
                return None
        if value is None or value is False:
            return None
        if isinstance(value, dict):
            self.errors.append(f"line {self.line}: object child is unsupported; use an explicit component")
            return None
        return TextNode(text=str(value), line=self.line)

    def _advance(self, count: int) -> None:
        segment = self.text[self.i : self.i + count]
        self.line += segment.count("\n")
        self.i += count


def parse_document(text: str) -> ParseResult:
    """解析一个 .slide 文件。"""
    parser = DSLParser(text)
    roots = parser.parse()
    result = ParseResult(
        root=roots[-1] if roots else None,
        warnings=list(parser.warnings),
        errors=list(parser.errors),
        slides=[r for r in roots if isinstance(r, Element) and r.tag.lower() == "slide"],
    )
    if not roots:
        result.errors.append("文件为空或未找到任何组件")
        return result
    if len(roots) != 1:
        result.errors.append("Expected exactly one top-level Slide component.")
    if len(result.slides) == 0:
        result.errors.append("未找到 <Slide> 组件（文件中最后一个表达式必须是 <Slide>）")
    elif len(result.slides) > 1:
        result.errors.append(f"一个 .slide 文件只允许一个 <Slide>，实际 {len(result.slides)} 个")
    elif isinstance(roots[-1], Element) and roots[-1].tag.lower() != "slide":
        result.errors.append("最后一个表达式必须是 <Slide> 组件")
    return result
