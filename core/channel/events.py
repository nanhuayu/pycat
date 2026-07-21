from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ChannelEvent:
    kind: str
    channel_id: str
    conversation_id: str = ""
    source: str = ""
    focus_requested: bool = False
    request_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


__all__ = ["ChannelEvent"]
