"""Utilities for parsing structured LLM output.

These helpers are intentionally small and deterministic. They do not summarize
or transform source content semantically; they only recover and normalize the
machine-readable envelope returned by an LLM.
"""
from __future__ import annotations

import json
from typing import Any, Iterable


def load_json_object(content: str) -> Any:
    """Parse a JSON object from model output.

    Accepts strict JSON, fenced JSON, or text that contains one top-level JSON
    object. Returns ``None`` when no object can be parsed.
    """
    text = str(content or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                value = json.loads(text[start : end + 1])
                return value if isinstance(value, dict) else None
            except Exception:
                return None
    return None


def one_line(value: Any, limit: int) -> str:
    """Normalize a value to one whitespace-collapsed line with a hard cap."""
    text = " ".join(str(value or "").split()).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def string_list(value: Any, *, limit: int, item_limit: int) -> list[str]:
    """Normalize JSON string/list fields into a deduplicated string list."""
    if isinstance(value, str):
        raw_items: Iterable[Any] = [value]
    elif isinstance(value, (list, tuple)):
        raw_items = value
    else:
        raw_items = []
    items: list[str] = []
    for item in raw_items:
        text = one_line(item, item_limit)
        if text and text not in items:
            items.append(text)
        if len(items) >= limit:
            break
    return items


def confidence(value: Any, default: float = 0.0) -> float:
    """Coerce a confidence-like value into the closed interval [0.0, 1.0]."""
    try:
        return max(0.0, min(1.0, float(value)))
    except Exception:
        return max(0.0, min(1.0, float(default)))
