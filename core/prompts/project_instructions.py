"""Project-level instruction loading for AGENTS.md."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List

from core.content.markdown import strip_frontmatter, trim_text


@dataclass(frozen=True)
class ProjectInstructionDocument:
    source: str
    content: str


class ProjectInstructionService:
    """Load root-level project instructions from ``AGENTS.md``.

    PyCat intentionally reads only the workspace root file. It does not walk up
    parent directories and does not merge nested files; this keeps instruction
    provenance simple and predictable for desktop-agent sessions.
    """

    FILENAME = "AGENTS.md"

    @classmethod
    def load(
        cls,
        work_dir: str | None,
        *,
        max_chars: int = 12_000,
    ) -> list[ProjectInstructionDocument]:
        root = cls._workspace_root(work_dir)
        if root is None:
            return []
        path = root / cls.FILENAME
        if not path.exists() or not path.is_file():
            return []
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return []
        content = strip_frontmatter(raw)
        if not content:
            return []
        return [ProjectInstructionDocument(source=str(path), content=trim_text(content, max_chars))]

    @classmethod
    def build_prompt_section(
        cls,
        work_dir: str | None,
        *,
        max_chars: int = 12_000,
    ) -> str:
        docs = cls.load(work_dir, max_chars=max_chars)
        if not docs:
            return ""
        lines: List[str] = ["<project_instructions>"]
        remaining = max(0, int(max_chars or 0))
        for doc in docs:
            header = f'<document source="{doc.source}">'
            footer = "</document>"
            budget = max(0, remaining - len(header) - len(footer) - 2)
            body = trim_text(doc.content, budget) if budget else ""
            if not body:
                continue
            lines.extend([header, body, footer])
            remaining -= len(header) + len(body) + len(footer) + 2
            if remaining <= 0:
                break
        lines.append("</project_instructions>")
        return "\n".join(lines) if len(lines) > 2 else ""

    @staticmethod
    def _workspace_root(work_dir: str | None) -> Path | None:
        raw = str(work_dir or "").strip()
        if not raw:
            return None
        try:
            root = Path(raw).expanduser().resolve()
        except Exception:
            return None
        if root.is_file():
            root = root.parent
        if not root.exists() or not root.is_dir():
            return None
        return root
