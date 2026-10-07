"""Shared execution context; Shell and Python use the same process owner."""
from pathlib import Path

from pycat.core.tools.base import ToolContext
from pycat.core.tools.system.process import CommandExecutor
from pycat.models.contracts.config import ShellConfig
from pycat.models.session_paths import resolve_session_root


def shell_config(context: ToolContext) -> ShellConfig:
    return getattr(getattr(context, "runtime", None), "shell_config", None) or ShellConfig()


def conversation_id(context: ToolContext) -> str:
    return str(getattr(getattr(context, "conversation", None), "id", "") or "")


def session_root(context: ToolContext) -> Path | None:
    """Canonical session root for process logs; None when no conversation is bound."""
    identity = conversation_id(context)
    if not identity:
        return None
    work_dir = str(
        getattr(getattr(context, "conversation", None), "work_dir", "")
        or getattr(context, "work_dir", "")
        or ""
    ).strip()
    return resolve_session_root(work_dir, identity, data_dir=context.data_dir)


def command_executor(context: ToolContext) -> CommandExecutor:
    manager = getattr(getattr(context, "runtime", None), "process_manager", None)
    if manager is None:
        raise RuntimeError("background process manager is unavailable in this tool context")
    return CommandExecutor(
        manager,
        shell_config=shell_config(context),
        conversation_id=conversation_id(context),
    )
