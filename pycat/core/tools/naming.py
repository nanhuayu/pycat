from __future__ import annotations

import re
from dataclasses import dataclass


_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9]*$")
_SEGMENT_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


@dataclass(frozen=True)
class ToolName:
    """Parsed public tool name.

    Normal built-in tools use ``<namespace>__<name>``. MCP tools keep the
    three-part public form ``mcp__<server>__<tool>`` because the server is a
    distinct routing segment.
    """

    namespace: str
    name: str
    server: str = ""

    @property
    def is_mcp(self) -> bool:
        return self.namespace == "mcp" and bool(self.server)

    @property
    def public(self) -> str:
        if self.is_mcp:
            return f"mcp__{self.server}__{self.name}"
        return f"{self.namespace}__{self.name}"

    @classmethod
    def parse(cls, value: str) -> "ToolName":
        raw = str(value or "").strip()
        parts = raw.split("__")
        if len(parts) == 2:
            namespace, name = parts
            if not _NAMESPACE_RE.fullmatch(namespace or ""):
                raise ValueError(f"Invalid tool namespace: {namespace!r}")
            if not _SEGMENT_RE.fullmatch(name or ""):
                raise ValueError(f"Invalid tool name segment: {name!r}")
            return cls(namespace=namespace, name=name)
        if len(parts) == 3 and parts[0] == "mcp":
            _, server, name = parts
            if not _SEGMENT_RE.fullmatch(server or ""):
                raise ValueError(f"Invalid MCP server segment: {server!r}")
            if not _SEGMENT_RE.fullmatch(name or ""):
                raise ValueError(f"Invalid MCP tool segment: {name!r}")
            return cls(namespace="mcp", server=server, name=name)
        raise ValueError(f"Invalid tool name: {raw!r}")

    @classmethod
    def build(cls, namespace: str, name: str) -> str:
        return cls.parse(f"{namespace}__{name}").public

    @classmethod
    def build_mcp(cls, server: str, name: str) -> str:
        return cls.parse(f"mcp__{server}__{name}").public


def is_valid_tool_name(value: str) -> bool:
    try:
        ToolName.parse(value)
        return True
    except ValueError:
        return False


def sanitize_tool_segment(value: str, *, allow_empty: bool = False) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", str(value or "").strip().lower())
    text = re.sub(r"_{2,}", "_", text).strip("_")
    if not text and allow_empty:
        return ""
    if not text:
        return "tool"
    if not text[0].isalpha():
        text = f"t_{text}"
    return text
