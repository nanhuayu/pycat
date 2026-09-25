"""Typed @ references; positions use Python character indices at the core boundary."""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pycat.core.commands.parser import inside_code

_IGNORE_NAMES = frozenset({".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache",
                           ".pytest_cache", ".tox", ".eggs", ".DS_Store", "Thumbs.db"})


class MentionKind(str, Enum):
    FILE = "file"
    COMMAND = "command"
    AGENT = "agent"
    RUN = "run"
    CHANNEL = "channel"
    CONTENT = "content"


@dataclass(frozen=True)
class MentionQuery:
    trigger: str
    prefix: str
    start_pos: int
    end_pos: int


@dataclass(frozen=True)
class MentionCandidate:
    label: str
    value: str
    kind: MentionKind = MentionKind.FILE
    terminal: bool = True
    insert_text: str = ""
    submit_on_accept: bool = False

    @property
    def key(self):
        return f"{self.kind.value}:{self.value}"

    def to_dict(self):
        return {"key": self.key, "label": self.label, "value": self.value, "kind": self.kind.value,
                "terminal": self.terminal, "insert_text": self.insert_text, "submit_on_accept": self.submit_on_accept}


def python_to_utf16(text: str, index: int) -> int:
    if not 0 <= index <= len(text):
        raise ValueError("Text position is out of range.")
    return len(text[:index].encode("utf-16-le")) // 2


def utf16_to_python(text: str, position: int) -> int:
    if position < 0:
        raise ValueError("Text position is out of range.")
    count = 0
    for index, char in enumerate(text):
        if count == position:
            return index
        count += 2 if ord(char) > 0xFFFF else 1
        if count > position:
            raise ValueError("Text position splits a Unicode character.")
    if count == position:
        return len(text)
    raise ValueError("Text position is out of range.")


def extract_mention_query(text: str, cursor_pos: int, *, triggers=("@",)):
    if not 0 <= cursor_pos <= len(text):
        return None
    start = text.rfind("\n", 0, cursor_pos) + 1
    for index in range(cursor_pos - 1, start - 1, -1):
        if text[index] not in triggers or index and not text[index - 1].isspace():
            continue
        if inside_code(text, index):
            return None
        prefix = text[index + 1:cursor_pos]
        if prefix.startswith('"'):
            prefix = prefix[1:]
            if '"' in prefix:
                return None
        elif any(char.isspace() for char in prefix):
            return None
        return MentionQuery(text[index], prefix, index, cursor_pos)
    return None


class MentionResolver:
    """One-directory, bounded, scope-checked completion. Never reads file contents."""
    def __init__(self, work_dir):
        self.work_dir = work_dir or ""

    def is_ready(self):
        return bool(self.work_dir) and Path(self.work_dir).is_dir()

    def _within(self, value):
        if not self.work_dir:
            raise ValueError("Select a workspace before adding a file reference.")
        root = Path(self.work_dir).resolve()
        path = (root / value).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Reference is outside the workspace.")
        return path

    def search(self, prefix, *, max_results=30):
        if not self.is_ready():
            return []
        prefix = prefix.replace("\\", "/")
        directory, _, name = prefix.rpartition("/")
        try:
            base = self._within(directory)
            results = []
            with os.scandir(base) as entries:
                for count, entry in enumerate(entries):
                    if count >= 4096:
                        break
                    if entry.name in _IGNORE_NAMES or entry.name.startswith(".") or name.casefold() not in entry.name.casefold():
                        continue
                    relative = (directory + "/" if directory else "") + entry.name
                    try:
                        path = self._within(relative)
                        if not path.exists():
                            continue
                        is_dir = path.is_dir()
                    except (OSError, ValueError):
                        continue
                    value = relative + ("/" if is_dir else "")
                    results.append(MentionCandidate(value, value, terminal=not is_dir,
                        insert_text='@"' + value if is_dir else ''))
            return sorted(results, key=lambda item: (item.terminal, item.value.casefold()))[:max(0, min(100, max_results))]
        except (OSError, ValueError):
            return []

    def resolve(self, display_name):
        value = display_name.value if isinstance(display_name, MentionCandidate) else display_name
        return str(self._within(value.rstrip("/")))
