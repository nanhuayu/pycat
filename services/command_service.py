from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from core.commands.types import ShellInvocation
from core.tools.permissions import ToolPermissionPolicy
from core.tools.process import CommandExecutionRequest, CommandExecutor, is_dangerous_command


class CommandExecutionDenied(Exception):
    """Raised when the user or policy denies an explicit shell command."""


class CommandService:
    """Executes explicit UI commands outside the LLM tool loop."""

    def __init__(self, executor: CommandExecutor | None = None) -> None:
        self._executor = executor or CommandExecutor()

    def execute_shell_invocation(
        self,
        invocation: ShellInvocation,
        *,
        work_dir: str,
        permission_policy: ToolPermissionPolicy,
        approval_callback: Optional[Callable[[str], bool]] = None,
        timeout_sec: int = 600,
    ) -> str:
        command_text = str(getattr(invocation, "command_text", "") or "").strip()
        if not command_text:
            raise ValueError("Missing shell command.")

        cwd = self._resolve_cwd(work_dir)
        denial = self._check_approval(
            command_text,
            cwd=cwd,
            permission_policy=permission_policy,
            approval_callback=approval_callback,
        )
        if denial:
            raise CommandExecutionDenied(denial)

        result = self._executor.execute(
            CommandExecutionRequest(
                command=command_text,
                cwd=cwd,
                timeout_sec=max(1, min(int(timeout_sec or 600), 600)),
                background=False,
            )
        )
        if result.timed_out:
            raise TimeoutError(f"Command timed out after {timeout_sec}s")
        return result.to_display_text(cwd)

    @staticmethod
    def _resolve_cwd(work_dir: str) -> Path:
        raw = str(work_dir or ".").strip() or "."
        return Path(raw).expanduser().resolve()

    @staticmethod
    def _check_approval(
        command_text: str,
        *,
        cwd: Path,
        permission_policy: ToolPermissionPolicy,
        approval_callback: Optional[Callable[[str], bool]],
    ) -> str | None:
        if is_dangerous_command(command_text):
            prompt = (
                "⚠️ DANGEROUS command detected!\n"
                f"> {command_text}\n\n"
                "This may cause irreversible changes. Approve?"
            )
            approved = bool(approval_callback(prompt)) if approval_callback else False
            return None if approved else "User denied dangerous command execution"

        if permission_policy.is_auto_approved("command"):
            return None

        prompt = f"Execute shell command in {cwd}?\n> {command_text}"
        approved = bool(approval_callback(prompt)) if approval_callback else False
        return None if approved else "User denied execution"
