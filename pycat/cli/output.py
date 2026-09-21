"""Versioned CLI output: final text, one JSON result, or NDJSON events."""
from __future__ import annotations

import json
import sys
import asyncio
import io
import os
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, TextIO

from pycat.models.contracts.agent import RunEvent, RunEventKind


def _json_default(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "value"):
        return value.value
    if is_dataclass(value):
        return asdict(value)
    return str(value)


@dataclass
class CliOutput:
    mode: str = "text"
    stream: TextIO | None = None
    err_stream: TextIO | None = None
    input_stream: TextIO | None = None
    interactive: bool | None = None

    def __post_init__(self):
        if self.mode not in {"text", "json", "stream-json"}:
            raise ValueError(f"Unknown output format: {self.mode}")
        self.stream = sys.stdout if self.stream is None else self.stream
        self.err_stream = sys.stderr if self.err_stream is None else self.err_stream
        self.input_stream = sys.stdin if self.input_stream is None else self.input_stream

    @property
    def json_mode(self):
        return self.mode in {"json", "stream-json"}

    @property
    def stream_json(self):
        return self.mode == "stream-json"

    def event(self, event: RunEvent):
        if self.stream_json:
            self.write_json({"version": 1, "type": "event", **event.to_dict()})
        elif event.kind == RunEventKind.ERROR:
            self.note(f"[error] {event.detail or event.data}")

    def token(self, token: str):
        if self.stream_json:
            self.write_json({"version": 1, "type": "token", "text": token})

    def thinking(self, text: str):
        if self.stream_json:
            self.write_json({"version": 1, "type": "thinking", "text": text})

    def final(self, *, status: str, message: str = "", error: str = "", conversation_id: str = "",
              run_id: str = "", stop_reason: str = "", deliveries: list[dict] | None = None):
        if self.json_mode:
            self.write_json({"version": 1, "type": "final", "status": status, "message": message,
                "error": error, "conversation_id": conversation_id, "run_id": run_id,
                "stop_reason": stop_reason, "deliveries": list(deliveries or ())})
        else:
            if message:
                print(message, file=self.stream)
            for item in deliveries or ():
                print(f"- {item.get('path') or item.get('ref') or item.get('name')}", file=self.stream)
            if error:
                self.note(f"[error] {error}")

    def write_json(self, payload):
        print(json.dumps(payload, ensure_ascii=False, default=_json_default), file=self.stream, flush=True)

    def note(self, message: str):
        print(str(message or ""), file=self.err_stream)

    def read_line(self, prompt: str = "") -> str | None:
        if not self.is_interactive:
            return None
        if prompt:
            print(prompt, end="", file=self.err_stream, flush=True)
        line = self.input_stream.readline()
        return line.rstrip("\r\n") if line else None

    async def read_line_async(self, prompt: str = "") -> str | None:
        """Cancellable console input without a blocked executor thread at exit."""
        if not self.is_interactive:
            return None
        if isinstance(self.input_stream, io.StringIO):
            return self.read_line(prompt)
        print(prompt, end="", file=self.err_stream, flush=True)
        if os.name == 'nt':
            import msvcrt
            chars = []
            while True:
                if not msvcrt.kbhit():
                    await asyncio.sleep(0.03)
                    continue
                char = msvcrt.getwch()
                if char == '\x03':
                    raise asyncio.CancelledError
                if char == '\x1a':
                    return None
                if char in {'\x00', '\xe0'}:
                    msvcrt.getwch()
                elif char in {'\r', '\n'}:
                    print(file=self.err_stream)
                    return ''.join(chars)
                elif char == '\b':
                    if chars:
                        chars.pop()
                        print('\b \b', end='', file=self.err_stream, flush=True)
                else:
                    chars.append(char)
                    print(char, end='', file=self.err_stream, flush=True)
        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        descriptor = self.input_stream.fileno()
        loop.add_reader(descriptor, lambda: not ready.done() and ready.set_result(True))
        try:
            await ready
            line = self.input_stream.readline()
            return line.rstrip('\r\n') if line else None
        finally:
            loop.remove_reader(descriptor)

    def result(self, value):
        if self.json_mode:
            self.write_json(value)
        elif isinstance(value, str):
            print(value, file=self.stream)
        else:
            print(json.dumps(value, ensure_ascii=False, indent=2, default=_json_default), file=self.stream)

    @property
    def is_interactive(self):
        if self.interactive is not None:
            return self.interactive
        return bool(getattr(self.input_stream, "isatty", lambda: False)()
                    and getattr(self.stream, "isatty", lambda: False)())
