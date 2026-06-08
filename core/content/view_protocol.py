"""Canonical content view labels and tool result view policies."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


SUMMARY_MODES = {"balanced", "brief", "detailed", "timeline", "evidence", "topic", "memory_candidates"}


class ArchivePolicy(Enum):
    INLINE = "inline"
    ARCHIVE = "archive"
    NEVER_ARCHIVE = "never_archive"


class ToolResultViewPolicy(Enum):
    INLINE = "inline"
    FULL_OR_SUMMARY = "full_or_summary"
    LINE_OR_SUMMARY = "line_or_summary"
    SUMMARY = "summary"
    NEVER_ARCHIVE = "never_archive"


class ContentExactness(Enum):
    EXACT = "exact"
    DERIVED = "derived"
    PENDING = "pending"


@dataclass(frozen=True)
class ContentViewLabel:
    kind: str
    desc: str = ""

    @property
    def value(self) -> str:
        if self.kind == "summary" and self.desc in {"", "balanced"}:
            return "summary"
        return f"{self.kind}:{self.desc}" if self.desc else self.kind

    @property
    def semantic_desc(self) -> str:
        if self.kind == "summary":
            return self.desc or "balanced"
        return self.desc

    @property
    def bracketed(self) -> str:
        return f"[{self.value}]"

    @classmethod
    def build(cls, kind: str, desc: str = "") -> str:
        return cls(str(kind or "").strip(), str(desc or "").strip()).bracketed

    @classmethod
    def parse(cls, raw: str) -> "ContentViewLabel":
        text = str(raw or "").strip()
        if text.startswith("[") and "]" in text:
            text = text[1:text.index("]")]
        if ":" in text:
            kind, desc = text.split(":", 1)
            return cls(kind.strip(), desc.strip())
        return cls(text.strip(), "")


def normalize_summary_mode(mode: object) -> str:
    raw = str(mode or "balanced").strip().lower()
    return raw if raw in SUMMARY_MODES else "balanced"


def summary_view_value(mode: object) -> str:
    return ContentViewLabel("summary", normalize_summary_mode(mode)).value


def summary_view_label(mode: object) -> str:
    return ContentViewLabel("summary", normalize_summary_mode(mode)).bracketed


def range_desc(start: object, end: object) -> str:
    return f"{int(start)}-{int(end)}"


def line_view_value(start: object, end: object) -> str:
    return ContentViewLabel("line", range_desc(start, end)).value


def char_view_value(start: object, end: object) -> str:
    return ContentViewLabel("char", range_desc(start, end)).value


def exact_view_from_text(tool_name: str, text: str) -> ContentViewLabel:
    if str(tool_name or "") == "file__read":
        match = re.match(r"^Lines\s+(\d+)-(\d+)\s+of\s+\d+:", str(text or ""))
        if match:
            return ContentViewLabel("line", f"{match.group(1)}-{match.group(2)}")
    return ContentViewLabel("full")
