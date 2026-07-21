"""Stable channel configuration contracts.

Connection state is intentionally absent from this module. It belongs to the
running gateway and must never be serialized with application settings.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping

from models.contracts.tooling import ToolSelectionPolicy


@dataclass(frozen=True)
class ChannelConfig:
    id: str = ""
    name: str = ""
    type: str = ""
    enabled: bool = True
    tool_selection: ToolSelectionPolicy | None = None
    source: str = ""
    mode_slug: str = "channel"
    session_id: str = ""
    config: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_dict(data: Mapping[str, Any] | None) -> "ChannelConfig":
        d = dict(data) if isinstance(data, Mapping) else {}
        channel_type = _as_str(d.get("type"), "").strip().lower()
        name = _as_str(d.get("name"), "").strip()
        source = _as_str(d.get("source"), "").strip()
        config = _stable_config(d.get("config"))

        return ChannelConfig(
            id=_as_str(d.get("id"), "").strip(),
            name=name,
            type=channel_type,
            enabled=_as_bool(d.get("enabled"), True),
            tool_selection=(
                ToolSelectionPolicy.from_dict(d.get("tool_selection"))
                if isinstance(d.get("tool_selection"), Mapping)
                else None
            ),
            source=source,
            mode_slug=_as_str(d.get("mode_slug"), "channel").strip().lower() or "channel",
            session_id=_as_str(d.get("session_id"), "").strip(),
            config=config,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "enabled": bool(self.enabled),
            "tool_selection": self.tool_selection.to_dict() if self.tool_selection is not None else None,
            "source": self.source,
            "mode_slug": self.mode_slug,
            "session_id": self.session_id,
            "config": _stable_config(self.config),
        }


def _stable_config(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    try:
        return str(value)
    except Exception:
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    try:
        return bool(value)
    except Exception:
        return default
