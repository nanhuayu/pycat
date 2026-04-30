"""Command dispatch helpers."""
from __future__ import annotations

from typing import Any, Dict, Optional

from core.commands.parser import find_command_invocation, parse_command_text
from core.commands.types import CommandAction, CommandResult, SlashCommand


def dispatch_command(
    text: str,
    *,
    commands: Dict[str, SlashCommand],
    context: Optional[Dict[str, Any]] = None,
) -> Optional[CommandResult]:
    """Execute a registered command and normalize legacy handler return values."""
    if not find_command_invocation(text):
        return None

    _prefix, cmd_name, args = parse_command_text(text)
    cmd = commands.get(cmd_name)
    if not cmd:
        return None

    raw = cmd.handler(args, context or {})
    if isinstance(raw, CommandResult):
        return raw
    if isinstance(raw, str):
        return CommandResult(action=CommandAction.DISPLAY, display_text=raw)
    return CommandResult(action=CommandAction.DISPLAY, display_text=str(raw))