"""Streaming runtime state models.

These are UI/runtime-only helpers (not persisted).
Keeping them in models/ makes them easy to import from UI and services.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import time
from typing import Any
import threading


logger = logging.getLogger(__name__)


@dataclass
class ConversationStreamState:
    """Per-conversation in-flight streaming state."""

    conversation_id: str
    request_id: str
    model: str = ""
    visible_text: str = ""
    thinking_text: str = ""
    last_event_kind: str = ""
    last_event_detail: str = ""
    active_tool: str = ""
    pending_tool_invocations: dict[str, dict[str, Any]] = field(default_factory=dict)
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def cancel(self) -> None:
        try:
            self.cancel_event.set()
        except Exception as exc:
            logger.debug("Failed to set conversation cancel event: %s", exc)

    def record_event(self, *, kind: str, detail: str = "", data: Any = None) -> None:
        self.last_event_kind = str(kind or "")
        self.last_event_detail = str(detail or "")
        item: dict[str, Any] = {
            "kind": self.last_event_kind,
            "detail": self.last_event_detail,
        }
        if isinstance(data, dict):
            item.update(data)
        item.setdefault("recorded_at", time.time())
        self.recent_events.append(item)
        if len(self.recent_events) > 20:
            self.recent_events = self.recent_events[-20:]

        phase = str(item.get("phase") or "").strip()
        tool_name = str(item.get("tool_name") or item.get("name") or "").strip()
        tool_call_id = str(item.get("tool_call_id") or "").strip()
        if tool_call_id:
            pending = self.pending_tool_invocations.setdefault(
                tool_call_id,
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "status": "running",
                    "started_at": item.get("recorded_at"),
                },
            )
            if tool_name:
                pending["tool_name"] = tool_name
            if kind == "tool_start":
                pending["status"] = "running"
                pending.setdefault("started_at", item.get("recorded_at"))
            elif kind == "step" and str(item.get("role") or "") == "tool_result":
                pending["status"] = "completed"
                pending["summary"] = str(item.get("summary") or "")
                pending["ended_at"] = item.get("recorded_at")
            elif kind == "tool_end":
                pending["status"] = "completed"
                pending["summary"] = str(item.get("summary") or pending.get("summary") or "")
                pending["ended_at"] = item.get("recorded_at")
                self.pending_tool_invocations.pop(tool_call_id, None)
        if kind == "tool_start" and tool_name:
            self.active_tool = tool_name
        elif kind == "tool_end" and (not tool_name or tool_name == self.active_tool):
            self.active_tool = ""
        elif kind in {"complete", "error"}:
            self.active_tool = ""
