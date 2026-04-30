"""Shared types for the command system."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Union


class CommandAction(str, Enum):
    """The type of action a command result triggers."""
    DISPLAY = "display"           # Show text to user in chat
    COMPACT = "compact"           # Trigger context condensation
    CLEAR = "clear"               # Clear conversation / new conversation
    MODE_SWITCH = "mode_switch"   # Switch mode (data = slug)
    PROMPT_RUN = "prompt_run"     # Run a prompt in the normal runtime chain
    SHELL_RUN = "shell_run"            # Run an explicit shell command in the current turn
    EXPORT = "export"             # Export conversation (data = format)


@dataclass(frozen=True)
class PromptInvocation:
    """Structured payload for command-triggered runtime execution."""

    content: str
    mode_slug: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    document_updates: Dict[str, Any] = field(default_factory=dict)
    source_prefix: str = "/"
    original_text: str = ""


@dataclass(frozen=True)
class ShellInvocation:
    """Structured payload for explicit shell execution."""

    command_text: str
    source_prefix: str = "!"
    original_text: str = ""


@dataclass
class CommandResult:
    """Structured result from a slash command."""
    action: CommandAction = CommandAction.DISPLAY
    data: Any = None
    display_text: str = ""


@dataclass(frozen=True)
class CommandPresentation:
    """UI-facing metadata derived from the same command definition."""

    usage: str = ""
    completion_text: str = ""
    menu_label: str = ""
    menu_tooltip: str = ""
    placeholder_hint: str = ""
    include_in_placeholder: bool = False
    takes_argument: bool = False


@dataclass
class SlashCommand:
    """A single slash command definition."""

    name: str
    description: str
    handler: Callable[..., Union[str, CommandResult]]
    presentation: CommandPresentation = field(default_factory=CommandPresentation)
