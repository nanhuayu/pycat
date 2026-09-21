"""Shared Markdown parsing helpers for PyCat runtime content.

The helpers in this module are intentionally small and deterministic.  They
cover the subset of Markdown/YAML-frontmatter behavior PyCat uses for skills,
memory notes, and project instructions without adding a runtime dependency on a
full Markdown or YAML parser.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import unquote

FRONTMATTER_RE = re.compile(r"^\ufeff?\s*---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Return ``(metadata, body)`` for a Markdown document.

    Supported metadata syntax is the simple frontmatter subset already used by
    PyCat: ``key: value``, ``key:`` followed by ``- item`` list lines, inline
    ``[a, b]`` lists, booleans, quoted strings, and pipe blocks. Unknown or
    malformed lines are ignored instead of raising.
    """
    raw = str(text or "")
    match = FRONTMATTER_RE.match(raw)
    if not match:
        return {}, raw

    metadata = _parse_frontmatter_block(match.group(1))
    return metadata, raw[match.end() :]


def strip_frontmatter(text: str) -> str:
    """Remove a leading frontmatter block from ``text`` if present."""
    _metadata, body = parse_frontmatter(text)
    return body.strip()


def render_frontmatter(metadata: Dict[str, Any]) -> str:
    """Render simple deterministic YAML frontmatter from ``metadata``.

    This intentionally supports the same small scalar/list subset accepted by
    :func:`parse_frontmatter`. Empty values are skipped so generated Markdown is
    compact and stable.
    """
    lines: list[str] = []
    for raw_key in sorted((metadata or {}).keys()):
        key = str(raw_key or "").strip().lower()
        if not key:
            continue
        value = metadata.get(raw_key)
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, (list, tuple, set)):
            items = [str(item).strip() for item in value if str(item).strip()]
            if not items:
                continue
            lines.append(f"{key}:")
            lines.extend(f"  - {_format_frontmatter_scalar(item)}" for item in items)
            continue
        lines.append(f"{key}: {_format_frontmatter_scalar(value)}")
    if not lines:
        return ""
    return "---\n" + "\n".join(lines) + "\n---\n\n"


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


def _parse_frontmatter_block(block: str) -> Dict[str, Any]:
    metadata: dict[str, Any] = {}
    lines = str(block or "").splitlines()
    index = 0
    while index < len(lines):
        raw_line = lines[index]
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            index += 1
            continue

        key, value = stripped.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if not key:
            index += 1
            continue

        if value in {"|", ">"}:
            block_lines: list[str] = []
            index += 1
            while index < len(lines):
                continuation = lines[index]
                if continuation.strip() and not continuation.startswith((" ", "\t")):
                    break
                block_lines.append(continuation.strip())
                index += 1
            metadata[key] = "\n".join(block_lines).strip()
            continue

        if not value:
            values: list[Any] = []
            index += 1
            while index < len(lines):
                candidate = lines[index].strip()
                if candidate.startswith("- "):
                    values.append(_parse_frontmatter_value(candidate[2:].strip()))
                    index += 1
                    continue
                if not candidate or candidate.startswith("#"):
                    index += 1
                    continue
                break
            metadata[key] = values
            continue

        metadata[key] = _parse_frontmatter_value(value)
        index += 1

    return metadata


def _parse_frontmatter_value(value: str) -> Any:
    raw = str(value or "").strip()
    if raw.startswith(("'", '"')) and raw.endswith(("'", '"')) and len(raw) >= 2:
        raw = raw[1:-1]
    if raw.startswith("[") and raw.endswith("]"):
        return [item.strip().strip("'\"") for item in raw[1:-1].split(",") if item.strip()]
    lowered = raw.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    return raw


def _format_frontmatter_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    raw = str(value).replace("\r", " ").replace("\n", " ").strip()
    if raw == "":
        return '""'
    needs_quote = raw.startswith(("[", "{", "-", "#", "!", "&", "*", "'", '"')) or ":" in raw or "#" in raw
    if raw.lower() in {"true", "false", "null", "none"}:
        needs_quote = True
    if not needs_quote:
        return raw
    return '"' + raw.replace('\\', '\\\\').replace('"', '\\"') + '"'
