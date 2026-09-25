"""Shared Markdown parsing helpers for PyCat runtime content.

Markdown documents share safe YAML frontmatter parsing and serialization.
Nested provenance and extension fields round-trip without a private format.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import unquote

import yaml


class _FrontmatterLoader(yaml.SafeLoader):
    """Keep timestamps portable strings instead of implicit datetime objects."""


_FrontmatterLoader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in values if tag != "tag:yaml.org,2002:timestamp"]
    for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
}

FRONTMATTER_RE = re.compile(r"^\ufeff?\s*---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def parse_frontmatter(text: str, *, strict: bool = False) -> Tuple[Dict[str, Any], str]:
    """Read safe YAML; strict document stores reject malformed metadata."""
    raw = str(text or "")
    match = FRONTMATTER_RE.match(raw)
    if not match:
        return {}, raw
    try:
        metadata = yaml.load(match.group(1), Loader=_FrontmatterLoader) or {}
        if not isinstance(metadata, dict) or any(not isinstance(key, str) for key in metadata):
            raise ValueError("frontmatter must be a string-keyed mapping")
    except (yaml.YAMLError, ValueError) as exc:
        if strict:
            raise ValueError(f"invalid YAML frontmatter: {exc}") from exc
        metadata = {}
    return metadata, raw[match.end():]


def strip_frontmatter(text: str) -> str:
    """Remove a leading frontmatter block from a Markdown document."""
    return parse_frontmatter(text)[1].strip()


def render_frontmatter(metadata: Dict[str, Any]) -> str:
    """Serialize structured metadata as readable, portable YAML."""
    values = {key: value for key, value in (metadata or {}).items() if value not in (None, "", [])}
    if not values:
        return ""
    block = yaml.safe_dump(values, allow_unicode=True, sort_keys=False, width=100).rstrip()
    return f"---\n{block}\n---\n\n"


def with_frontmatter(content: str, metadata: Dict[str, Any]) -> str:
    """Return Markdown body with normalized leading frontmatter."""
    body = strip_frontmatter(str(content or ""))
    prefix = render_frontmatter(metadata)
    return f"{prefix}{body}" if prefix else body


def extract_markdown_links(text: str) -> List[str]:
    """Extract safe relative Markdown link targets.

    This function does not perform filesystem checks. It only normalizes link
    strings and filters out external URLs, mailto links, absolute paths, and
    pure anchors. Callers are still responsible for resolving against a root and
    checking path traversal.
    """
    links: list[str] = []
    seen: set[str] = set()
    for match in MARKDOWN_LINK_RE.finditer(str(text or "")):
        target = str(match.group(1) or "").strip()
        if not target:
            continue
        target = unquote(target.split("#", 1)[0].split("?", 1)[0].strip())
        lowered = target.lower()
        if not target or "://" in target or lowered.startswith("mailto:"):
            continue
        normalized = target.replace("\\", "/")
        if normalized.startswith("./"):
            normalized = normalized[2:]
        if normalized.startswith("/") or normalized.startswith("#"):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        links.append(normalized)
    return links


def extract_title_and_preview(
    path_or_name: str | Path,
    content: str,
    *,
    max_body_chars: int = 700,
    max_preview_chars: int = 900,
) -> tuple[str, str]:
    """Extract a human title and compact preview from Markdown content."""
    name = Path(path_or_name).name if isinstance(path_or_name, Path) else str(path_or_name or "")
    stem = Path(name).stem if name else "document"
    body = strip_frontmatter(str(content or ""))
    lines = [line.strip() for line in body.splitlines()]
    title = ""
    body_lines: list[str] = []
    for line in lines:
        if not line:
            if body_lines:
                break
            continue
        if not title and line.startswith("#"):
            title = line.lstrip("#").strip()
            continue
        body_lines.append(line)
        if len(" ".join(body_lines)) >= max_body_chars:
            break
    title = title or stem
    preview = " ".join(body_lines).strip() or body.strip()
    return f"{name} / {title}" if name else title, trim_text(preview, max_preview_chars)


def trim_text(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    raw = str(text or "").strip()
    if len(raw) <= max_chars:
        return raw
    return raw[: max(0, max_chars - 3)].rstrip() + "..."
