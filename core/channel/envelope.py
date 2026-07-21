from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any

from models.conversation import Message


SAFE_META_KEY = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
CHANNEL_RE = re.compile(
    r"<channel\s+source=\"([^\"]+)\"([^>]*)>\s*(.*?)\s*</channel>",
    re.DOTALL,
)
ATTR_RE = re.compile(r"\s+([a-zA-Z_][a-zA-Z0-9_]*)=\"([^\"]*)\"")


@dataclass(frozen=True)
class ChannelOrigin:
    """Source metadata for a message injected from an external channel."""

    source: str
    user: str = ""
    thread_id: str = ""
    message_id: str = ""
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        leaf = self.source.rsplit(":", 1)[-1] if self.source else "channel"
        if self.user:
            return f"{leaf} / {self.user}"
        return leaf


@dataclass(frozen=True)
class ChannelEnvelope:
    origin: ChannelOrigin
    content: str


@dataclass(frozen=True)
class ChannelInbound:
    """Normalized inbound payload from an external channel adapter."""

    origin: ChannelOrigin
    content: str

    def to_message(self) -> Message:
        return message_from_channel(
            self.origin.source,
            self.content,
            {
                **(self.origin.meta or {}),
                "user": self.origin.user,
                "thread_id": self.origin.thread_id,
                "message_id": self.origin.message_id,
            },
        )


def _safe_meta(meta: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(meta, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in meta.items():
        name = str(key or "").strip()
        if not SAFE_META_KEY.match(name):
            continue
        text = str(value or "").strip()
        if text:
            out[name] = text
    return out


def _clean_source(source: str) -> str:
    return str(source or "channel").strip() or "channel"


def channel_metadata(source: str, meta: dict[str, Any] | None = None) -> dict[str, str]:
    """Return sanitized metadata suitable for ``Message.metadata['channel']``."""

    safe = _safe_meta(meta)
    safe["source"] = _clean_source(source)
    return safe


def message_from_channel(source: str, content: str, meta: dict[str, Any] | None = None) -> Message:
    """Create a first-class user message from external channel input."""

    return Message(
        role="user",
        content=str(content or ""),
        metadata={
            "channel": channel_metadata(source, meta),
            "external_input": True,
        },
    )


def wrap_channel_message(source: str, content: str, meta: dict[str, Any] | None = None) -> str:
    """Wrap inbound channel text in a safe XML-like envelope."""

    clean_source = html.escape(_clean_source(source), quote=True)
    attrs = []
    for key, value in _safe_meta(meta).items():
        attrs.append(f' {key}="{html.escape(value, quote=True)}"')
    body = html.escape(str(content or ""), quote=False)
    return f'<channel source="{clean_source}"{"".join(attrs)}>\n{body}\n</channel>'


def parse_channel_message(content: str) -> ChannelEnvelope | None:
    raw = str(content or "")
    match = CHANNEL_RE.search(raw)
    if not match:
        return None

    source = html.unescape(match.group(1)).strip()
    attr_text = match.group(2) or ""
    body = html.unescape(match.group(3) or "").strip()
    meta: dict[str, str] = {}
    for attr_match in ATTR_RE.finditer(attr_text):
        key = attr_match.group(1)
        if not SAFE_META_KEY.match(key):
            continue
        meta[key] = html.unescape(attr_match.group(2)).strip()

    origin = ChannelOrigin(
        source=source or "channel",
        user=meta.get("user", ""),
        thread_id=meta.get("thread_id", meta.get("chat_id", "")),
        message_id=meta.get("message_id", ""),
        meta=meta,
    )
    return ChannelEnvelope(origin=origin, content=body)


def channel_origin_from_message(message: Message) -> ChannelOrigin | None:
    metadata = getattr(message, "metadata", {}) or {}
    channel_meta = metadata.get("channel") if isinstance(metadata, dict) else None
    if isinstance(channel_meta, dict):
        safe = _safe_meta(channel_meta)
        source = safe.get("source") or safe.get("server") or safe.get("name") or "channel"
        return ChannelOrigin(
            source=source,
            user=safe.get("user", ""),
            thread_id=safe.get("thread_id", safe.get("chat_id", "")),
            message_id=safe.get("message_id", ""),
            meta=safe,
        )

    parsed = parse_channel_message(getattr(message, "content", "") or "")
    if parsed is None:
        return None
    return parsed.origin


def channel_value(message: Message, key: str) -> str:
    origin = channel_origin_from_message(message)
    if origin is None:
        return ""
    key_text = str(key or "").strip()
    if key_text == "source":
        return origin.source
    if key_text == "user":
        return origin.user
    if key_text == "thread_id":
        return origin.thread_id
    if key_text == "message_id":
        return origin.message_id
    return str((origin.meta or {}).get(key_text, "") or "").strip()


__all__ = [
    "ChannelEnvelope",
    "ChannelInbound",
    "ChannelOrigin",
    "channel_metadata",
    "channel_origin_from_message",
    "channel_value",
    "message_from_channel",
    "parse_channel_message",
    "wrap_channel_message",
]
