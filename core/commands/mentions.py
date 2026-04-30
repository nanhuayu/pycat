"""Inline mention helpers.

Supports ``#`` file references. Slash command completion is handled outside of
the inline mention system.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from typing import Iterable, List, Optional

# Directories/files to always exclude from completion
_IGNORE_NAMES = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".tox", ".eggs",
    ".DS_Store", "Thumbs.db",
})


class MentionKind(str, Enum):
    """Supported inline mention kinds."""

    FILE = "file"
    COMMAND = "command"


@dataclass(frozen=True)
class MentionQuery:
    """A parsed inline mention under the current cursor."""

    trigger: str
    prefix: str
    start_pos: int
    end_pos: int


@dataclass(frozen=True)
class MentionCandidate:
    """A selectable mention candidate shown in the UI."""

    label: str
    value: str
    kind: MentionKind = MentionKind.FILE
    terminal: bool = True
    insert_text: str = ""


def extract_mention_query(
    text: str,
    cursor_pos: int,
    *,
    triggers: Iterable[str] = ("#",),
) -> Optional[MentionQuery]:
    """Return the active inline mention query at *cursor_pos*.

    A valid query must:
    - start at line start or after whitespace
    - contain no spaces between trigger and cursor
    """
    if cursor_pos < 0 or cursor_pos > len(text):
        return None

    line_start = text.rfind("\n", 0, cursor_pos) + 1
    current_line_text = text[line_start:cursor_pos]

    best: Optional[MentionQuery] = None
    for trigger in triggers:
        idx = current_line_text.rfind(trigger)
        if idx == -1:
            continue
        if idx > 0 and not current_line_text[idx - 1].isspace():
            continue
        prefix = current_line_text[idx + 1 :]
        if any(ch.isspace() for ch in prefix):
            continue
        best = MentionQuery(
            trigger=trigger,
            prefix=prefix,
            start_pos=line_start + idx,
            end_pos=cursor_pos,
        )
    return best


class MentionResolver:
    """Resolve ``#`` file mentions against a working directory.

    Usage::

        resolver = MentionResolver("/path/to/project")
        matches = resolver.search("main")      # ["main.py", "main_test.py"]
        full    = resolver.resolve("main.py")   # "/path/to/project/main.py"

    Supports sub-path navigation: ``#src/util`` searches inside ``src/``.
    """

    def __init__(self, work_dir: str) -> None:
        self._work_dir = work_dir or ""

    @property
    def work_dir(self) -> str:
        return self._work_dir

    @work_dir.setter
    def work_dir(self, value: str) -> None:
        self._work_dir = value or ""

    def is_ready(self) -> bool:
        """Return True if work_dir is set and exists."""
        return bool(self._work_dir) and os.path.isdir(self._work_dir)

    def search(self, prefix: str, *, max_results: int = 30) -> List[MentionCandidate]:
        """Search for files/directories matching *prefix*.

        - Case-insensitive substring match.
        - If *prefix* contains ``/``, the part before the last ``/`` is
          treated as a subdirectory and the remainder as the name filter.
        - Returns display names (relative to *work_dir*).  Directories
          end with ``/`` so the UI can distinguish them.
        """
        if not self.is_ready():
            return []

        # Split prefix into a sub-path and a name filter.
        if "/" in prefix:
            sub_dir, _, name_filter = prefix.rpartition("/")
            base = os.path.join(self._work_dir, sub_dir)
            rel_prefix = sub_dir + "/"
        else:
            base = self._work_dir
            name_filter = prefix
            rel_prefix = ""

        if not os.path.isdir(base):
            return []

        name_lower = name_filter.lower()
        results: list[MentionCandidate] = []

        try:
            entries = os.listdir(base)
        except OSError:
            return []

        entries.sort()

        for entry in entries:
            if entry in _IGNORE_NAMES or entry.startswith("."):
                continue
            full = os.path.join(base, entry)
            if os.path.isdir(full):
                display = rel_prefix + entry + "/"
                if name_lower in entry.lower():
                    results.append(
                        MentionCandidate(
                            label=display,
                            value=display,
                            kind=MentionKind.FILE,
                            terminal=False,
                        )
                    )
            elif os.path.isfile(full):
                if name_lower in entry.lower():
                    display = rel_prefix + entry
                    results.append(
                        MentionCandidate(
                            label=display,
                            value=display,
                            kind=MentionKind.FILE,
                            terminal=True,
                        )
                    )

            if len(results) >= max_results:
                break

        return results

    def resolve(self, display_name: str | MentionCandidate) -> str:
        """Convert a display name to an absolute path.

        Strips trailing ``/`` for directories.
        """
        raw_name = display_name.value if isinstance(display_name, MentionCandidate) else display_name
        clean = raw_name.rstrip("/")
        return os.path.join(self._work_dir, clean)
