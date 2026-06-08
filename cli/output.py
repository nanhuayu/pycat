from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, TextIO

from core.runtime.events import TurnEvent, TurnEventKind


def _json_default(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "value"):
        return value.value
    return str(value)


@dataclass
class CliOutput:
    """Terminal output adapter for TurnEngine events."""

    mode: str = "text"
    stream: TextIO | None = None
    err_stream: TextIO | None = None
    _token_started: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.stream is None:
            self.stream = sys.stdout
        if self.err_stream is None:
            self.err_stream = sys.stderr

    @property
    def json_mode(self) -> bool:
        return str(self.mode or "text").lower() == "json"

    def event(self, event: TurnEvent) -> None:
        if self.json_mode:
            self.write_json({
                "type": "event",
                "kind": getattr(event.kind, "value", str(event.kind)),
                "turn": event.turn,
                "detail": event.detail,
                "tool_name": event.tool_name,
                "data": self._event_data(event.data),
            })
            return

        if event.kind == TurnEventKind.TOOL_START:
            self._finish_token_line()
            name = event.tool_name or "tool"
            print(f"[tool:start] {name}", file=self.err_stream)
        elif event.kind == TurnEventKind.TOOL_END:
            self._finish_token_line()
            name = event.tool_name or "tool"
            print(f"[tool:end] {name}", file=self.err_stream)
        elif event.kind == TurnEventKind.ERROR:
            self._finish_token_line()
            print(f"[error] {event.detail or event.data}", file=self.err_stream)

    def token(self, token: str) -> None:
        if self.json_mode:
            self.write_json({"type": "token", "text": str(token or "")})
            return
        self._token_started = True
        print(str(token or ""), end="", file=self.stream, flush=True)

    def thinking(self, text: str) -> None:
        if self.json_mode:
            self.write_json({"type": "thinking", "text": str(text or "")})

    def final(self, *, status: str, message: str = "", error: str = "", conversation_id: str = "") -> None:
        if self.json_mode:
            self.write_json({
                "type": "final",
                "status": status,
                "message": message,
                "error": error,
                "conversation_id": conversation_id,
            })
            return

        self._finish_token_line()
        if message:
            print(message, file=self.stream)
        if error:
            print(f"[error] {error}", file=self.err_stream)

    def write_json(self, payload: dict[str, Any]) -> None:
        print(json.dumps(payload, ensure_ascii=False, default=_json_default), file=self.stream, flush=True)

    def _finish_token_line(self) -> None:
        if self._token_started:
            print("", file=self.stream, flush=True)
            self._token_started = False

    @staticmethod
    def _event_data(data: Any) -> Any:
        if hasattr(data, "to_dict"):
            return data.to_dict()
        if isinstance(data, (str, int, float, bool)) or data is None:
            return data
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return data
        return str(data)
