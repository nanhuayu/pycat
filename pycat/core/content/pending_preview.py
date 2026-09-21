from __future__ import annotations

from pycat.core.content.markdown import trim_text


def build_pending_preview(
    text: str,
    *,
    tool_name: str = "",
    min_chars: int = 2_000,
    max_chars: int = 8_000,
    default_chars: int = 4_000,
) -> tuple[str, str]:
    raw = str(text or "").strip()
    if not raw:
        return "", ""
    limit = max(min_chars, min(max_chars, default_chars))
    _ = tool_name
    return trim_text(raw, limit), f"char excerpt, up to {limit} chars"
