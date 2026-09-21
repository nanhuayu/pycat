"""Shared types for the command system."""
from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
from enum import Enum
from typing import Any, Callable, Dict, Union


class CommandAction(str, Enum):
    """The type of action a command result triggers."""
    DISPLAY = "display"           # Show text to user in chat
    COMPACT = "compact"           # Trigger context condensation
    CLEAR = "clear"               # Clear conversation / new conversation
    MODE_SWITCH = "mode_switch"   # Switch mode (data = slug)
    PROMPT_RUN = "prompt_run"     # Run a prompt in the normal runtime chain
    EXPORT = "export"             # Export conversation (data = format)
    SHELL_RUN = "shell_run"       # Execute an explicit shell command
    MODEL_SWITCH = "model_switch"
    RESUME = "resume"
    RENAME = "rename"
    OPEN_PANEL = "open_panel"
    EXIT = "exit"


@dataclass(frozen=True)
class PromptInvocation:
    """Structured payload for command-triggered runtime execution."""

    content: str
    mode_slug: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    source_prefix: str = "/"
    original_text: str = ""
    delegate_profile: str = ""

    def message_metadata(self) -> dict[str, Any]:
        if set(self.metadata) - {"command_run", "skill_run"}:
            raise ValueError("Unsupported invocation metadata.")
        metadata = deepcopy(self.metadata)
        command = metadata.setdefault('command_run', {})
        command.setdefault('source_prefix', self.source_prefix)
        command.setdefault('original_text', self.original_text)
        return metadata


@dataclass(frozen=True)
class ShellInvocation:
    """Structured payload for explicit ``!`` shell execution."""

    command: str
    cwd: str = ""
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
    submit_on_accept: bool = False


@dataclass
class SlashCommand:
    """A single slash command definition."""

    name: str
    description: str
    handler: Callable[..., Union[str, CommandResult]]
    presentation: CommandPresentation = field(default_factory=CommandPresentation)
    aliases: tuple[str, ...] = ()
