"""Skill resource resolution and reading."""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from core.content.markdown import extract_markdown_links
from models.contracts.skill import Skill


def resolve_skill_root(path: Path) -> Path:
    return path.parent if path.is_file() else path


def list_skill_resource_paths(skill: Skill) -> List[str]:
    root = resolve_skill_root(Path(skill.source))
    results: List[str] = []

    for relative_path in extract_markdown_links(skill.content):
        candidate = (root / relative_path).resolve()
        try:
            candidate.relative_to(root.resolve())
        except Exception:
            continue
        if candidate.exists() and candidate.is_file():
            results.append(candidate.relative_to(root).as_posix())

    for directory_name in ("references", "templates", "scripts", "assets"):
        directory = root / directory_name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                results.append(path.relative_to(root).as_posix())

    return dedupe_preserve_order(results)


def resolve_skill_resource_path(skill: Skill, relative_path: str) -> Optional[Path]:
    root = resolve_skill_root(Path(skill.source)).resolve()
    normalized = str(relative_path or "").strip().replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized:
        return None
    candidate = (root / normalized).resolve()
    try:
        candidate.relative_to(root)
    except Exception:
        return None
    return candidate


def read_skill_resource(
    skill: Skill,
    relative_path: str,
    *,
    start_line: int = 1,
    end_line: Optional[int] = None,
) -> Optional[Tuple[str, int, int, int]]:
    path = resolve_skill_resource_path(skill, relative_path)
    if path is None or not path.is_file():
        return None

    text = read_text_with_fallback(path)
    lines = text.splitlines()
    total_lines = len(lines)
    if total_lines == 0:
        return "", 0, 0, 0

    safe_start = max(1, int(start_line or 1))
    safe_end = total_lines if end_line is None else min(total_lines, max(safe_start, int(end_line or safe_start)))
    snippet = "\n".join(lines[safe_start - 1 : safe_end])
    return snippet, total_lines, safe_start, safe_end


def read_text_with_fallback(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "gbk", "mbcs"):
        try:
            return raw.decode(encoding)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


def dedupe_preserve_order(values: list[str] | tuple[str, ...]) -> List[str]:
    result: List[str] = []
    seen: set[str] = set()
    for raw_value in values:
        value = str(raw_value or "").strip()
        if not value:
            continue
        lowered = value.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        result.append(value)
    return result
