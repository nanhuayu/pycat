"""Skill manifest loading helpers."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.config import get_global_subdir
from core.content.markdown import parse_frontmatter
from models.contracts.skill import Skill

logger = logging.getLogger(__name__)


def external_user_skill_dirs() -> List[Path]:
    home = Path.home()
    return [
        home / ".agents" / "skills",
        home / ".claude" / "skills",
        home / ".codex" / "skills",
    ]


def skill_dir_scope(directory: Path, work_dir: str) -> Tuple[str, bool]:
    resolved = directory.resolve()
    project_dir = (Path(work_dir or ".").resolve() / ".pycat" / "skills").resolve()
    global_dir = get_global_subdir("skills").resolve()
    try:
        if resolved == project_dir:
            return "project", False
        if resolved == global_dir:
            return "global", False
    except Exception:
        pass
    return "external", True


def load_skill_file(
    path: Path,
    *,
    default_name: str,
    enabled: bool = True,
    source_scope: str = "project",
    read_only: bool = False,
) -> Optional[Skill]:
    content = path.read_text(encoding="utf-8")
    metadata, body = parse_frontmatter(content)
    raw_name = str(default_name or "").strip().lower()
    if not raw_name:
        return None
    declared_name = str(metadata.get("name") or "").strip().lower()
    if declared_name:
        metadata = dict(metadata)
        metadata.setdefault("declared-name", declared_name)
        if declared_name != raw_name:
            logger.warning(
                "Skill frontmatter name mismatch for %s: declared %r, using directory name %r",
                path,
                declared_name,
                raw_name,
            )
    tags = extract_tags(body)
    frontmatter_tags = metadata.get("tags")
    if isinstance(frontmatter_tags, list):
        tags = [str(tag).strip() for tag in frontmatter_tags if str(tag).strip()] or tags
    return Skill(
        name=raw_name,
        content=body.strip() or content.strip(),
        source=str(path),
        description=str(metadata.get("description") or "").strip(),
        tags=tags,
        metadata=metadata,
        enabled=bool(enabled),
        source_scope=str(source_scope or "project"),
        read_only=bool(read_only),
    )


def parse_skill_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    return parse_frontmatter(content)


def parse_skill_frontmatter_value(value: str) -> Any:
    raw = str(value or "").strip()
    if raw.startswith(("'", '"')) and raw.endswith(("'", '"')) and len(raw) >= 2:
        raw = raw[1:-1]
    if raw.startswith("[") and raw.endswith("]"):
        return [
            item.strip().strip("'\"")
            for item in raw[1:-1].split(",")
            if item.strip()
        ]
    lowered = raw.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    return raw


def extract_tags(content: str) -> List[str]:
    for line in content.splitlines()[:10]:
        stripped = line.strip()
        if stripped.lower().startswith("tags:"):
            raw = stripped[5:].strip().strip("[]")
            return [tag.strip() for tag in raw.split(",") if tag.strip()]
    return []
