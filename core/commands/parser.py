"""Command parsing helpers for explicit ``/`` commands."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


COMMAND_PREFIXES = ("/", "!")
_COMMAND_NAME_EXTRA_CHARS = {"-", "_", "."}


@dataclass(frozen=True)
class CommandInvocation:
    prefix: str
    command: str
    args: str
    start_pos: int
    token_end_pos: int
    leading_text: str = ""
    trailing_text: str = ""

    @property
    def surrounding_text(self) -> str:
        return " ".join(
            part for part in (self.leading_text.strip(), self.trailing_text.strip()) if part
        ).strip()


def _is_command_name_char(ch: str) -> bool:
    return ch.isalnum() or ch in _COMMAND_NAME_EXTRA_CHARS


def find_command_invocation(
    text: str,
    *,
    prefixes: Iterable[str] = COMMAND_PREFIXES,
) -> Optional[CommandInvocation]:
    raw = str(text or "")
    best: Optional[CommandInvocation] = None

    for index, ch in enumerate(raw):
        if ch not in prefixes:
            continue
        if index > 0 and not raw[index - 1].isspace():
            continue

        cursor = index + 1
        while cursor < len(raw) and _is_command_name_char(raw[cursor]):
            cursor += 1
        if cursor == index + 1:
            continue
        if cursor < len(raw) and not raw[cursor].isspace():
            continue

        best = CommandInvocation(
            prefix=ch,
            command=raw[index + 1 : cursor].strip().lower(),
            args=raw[cursor:].strip(),
            start_pos=index,
            token_end_pos=cursor,
            leading_text=raw[:index].strip(),
            trailing_text=raw[cursor:].strip(),
        )

    return best


def extract_command_query(
    text: str,
    cursor_pos: int,
    *,
    prefixes: Iterable[str] = COMMAND_PREFIXES,
) -> Optional[tuple[str, str, int, int]]:
    if cursor_pos < 0 or cursor_pos > len(text):
        return None

    line_start = text.rfind("\n", 0, cursor_pos) + 1
    current_line = text[line_start:cursor_pos]
    best: Optional[tuple[str, str, int, int]] = None

    for trigger in prefixes:
        idx = current_line.rfind(trigger)
        if idx == -1:
            continue
        if idx > 0 and not current_line[idx - 1].isspace():
            continue

        prefix = current_line[idx + 1 :]
        best = (trigger, prefix, line_start + idx, cursor_pos)

    return best


def get_command_prefix(text: str) -> str:
    invocation = find_command_invocation(text)
    return invocation.prefix if invocation else ""


def is_command_text(text: str) -> bool:
    return bool(get_command_prefix(text))


def parse_command_text(text: str) -> tuple[str, str, str]:
    """Parse ``/command args`` → ``(prefix, command, args)``."""
    invocation = find_command_invocation(text)
    if not invocation:
        return ("", "", str(text or "").strip())
    return (invocation.prefix, invocation.command, invocation.args.strip())


def parse_slash_command(text: str) -> tuple[str, str]:
    """Backward-compatible slash parser wrapper."""
    _prefix, cmd_name, args = parse_command_text(text)
    return (cmd_name, args)


def is_slash_command(text: str) -> bool:
    return get_command_prefix(text) == "/"