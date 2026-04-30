from typing import Any, Dict
from core.tools.base import BaseTool, ToolContext, ToolResult
from core.tools.process import CommandExecutionRequest, CommandExecutor, is_dangerous_command


def _coerce_timeout(value: Any, default: int = 600, maximum: int = 600) -> int:
    try:
        timeout_sec = int(value or default)
    except Exception:
        timeout_sec = default
    timeout_sec = max(1, timeout_sec)
    return min(timeout_sec, maximum)


class _CommandApprovalMixin:
    def __init__(self) -> None:
        self._approved_commands = set()

    async def _approve_command(self, command: str, cwd_path, context: ToolContext) -> str | None:
        dangerous = is_dangerous_command(command)
        cmd_key = f"{command}@{cwd_path}"

        if dangerous:
            approved = await context.ask_approval(
                f"⚠️ DANGEROUS command detected!\n> {command}\n\nThis may cause irreversible changes. Approve?"
            )
            if not approved:
                return "User denied dangerous command execution"
            return None

        if cmd_key in self._approved_commands:
            return None

        approved = await context.ask_approval(f"Execute shell command in {cwd_path}?\n> {command}")
        if not approved:
            return "User denied execution"

        self._approved_commands.add(cmd_key)
        return None


class _ProcessReadTool(BaseTool):
    @property
    def group(self) -> str:
        return "command"

    @property
    def category(self) -> str:
        return "read"

class ExecuteCommandTool(BaseTool):
    def __init__(self):
        super().__init__()
        self._approval = _CommandApprovalMixin()
        self._executor = CommandExecutor()

    @property
    def name(self) -> str:
        return "execute_command"

    @property
    def description(self) -> str:
        return (
            "Execute a shell command in the workspace. "
            "Supports timeout, background execution, and output truncation. "
            "Dangerous commands (rm -rf, git push --force, etc.) require explicit user approval."
        )

    @property
    def group(self) -> str:
        return "command"

    @property
    def category(self) -> str:
        return "command"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "cwd": {"type": "string", "description": "Working directory (default '.')"},
                "timeout": {
                    "type": "number",
                    "description": "Timeout in seconds (default 600, max 600)",
                },
                "background": {
                    "type": "boolean",
                    "description": "Run in background (returns immediately, default false)",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        command = (arguments.get("command", "") or "").strip()
        cwd_str = arguments.get("cwd", ".")
        timeout_sec = _coerce_timeout(arguments.get("timeout", 600))
        background = bool(arguments.get("background", False))

        if not command:
            return ToolResult("Missing 'command'", is_error=True)

        try:
            cwd_path = context.resolve_path(cwd_str)
        except Exception as e:
            return ToolResult(str(e), is_error=True)

        denial = await self._approval._approve_command(command, cwd_path, context)
        if denial:
            return ToolResult(denial, is_error=True)

        try:
            result = self._executor.execute(
                CommandExecutionRequest(
                    command=command,
                    cwd=cwd_path,
                    timeout_sec=timeout_sec,
                    background=background,
                )
            )
            if result.timed_out:
                return ToolResult(
                    f"Command timed out after {timeout_sec}s. Use 'background: true' for long-running commands.",
                    is_error=True,
                )
            return ToolResult(result.to_display_text(cwd_path))
        except Exception as e:
            return ToolResult(f"Execution error: {e}", is_error=True)


class ShellStartTool(BaseTool):
    def __init__(self):
        super().__init__()
        self._approval = _CommandApprovalMixin()
        self._executor = CommandExecutor()

    @property
    def name(self) -> str:
        return "shell_start"

    @property
    def description(self) -> str:
        return "Start a long-running shell command in the background and return a process_id for later management."

    @property
    def group(self) -> str:
        return "command"

    @property
    def category(self) -> str:
        return "command"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to start"},
                "cwd": {"type": "string", "description": "Working directory (default '.')"},
            },
            "required": ["command"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        command = (arguments.get("command", "") or "").strip()
        cwd_str = arguments.get("cwd", ".")
        if not command:
            return ToolResult("Missing 'command'", is_error=True)
        try:
            cwd_path = context.resolve_path(cwd_str)
        except Exception as e:
            return ToolResult(str(e), is_error=True)

        denial = await self._approval._approve_command(command, cwd_path, context)
        if denial:
            return ToolResult(denial, is_error=True)

        try:
            result = self._executor.execute(
                CommandExecutionRequest(command=command, cwd=cwd_path, background=True)
            )
            return ToolResult(result.to_display_text(cwd_path))
        except Exception as e:
            return ToolResult(f"Execution error: {e}", is_error=True)


class ShellStatusTool(_ProcessReadTool):
    def __init__(self):
        super().__init__()
        self._executor = CommandExecutor()

    @property
    def name(self) -> str:
        return "shell_status"

    @property
    def description(self) -> str:
        return "Get the status of a background shell process by process_id."

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "process_id": {"type": "string", "description": "Background process id returned by shell_start or execute_command(background=true)"},
            },
            "required": ["process_id"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        process_id = (arguments.get("process_id", "") or "").strip()
        if not process_id:
            return ToolResult("Missing 'process_id'", is_error=True)
        try:
            return ToolResult(self._executor.status(process_id).to_display_text())
        except Exception as e:
            return ToolResult(f"Status error: {e}", is_error=True)


class ShellLogsTool(_ProcessReadTool):
    def __init__(self):
        super().__init__()
        self._executor = CommandExecutor()

    @property
    def name(self) -> str:
        return "shell_logs"

    @property
    def description(self) -> str:
        return "Read recent logs from a background shell process by process_id."

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "process_id": {"type": "string", "description": "Background process id"},
                "tail_bytes": {"type": "number", "description": "How many bytes to read from the end of the log (default 12000, max 50000)"},
            },
            "required": ["process_id"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        process_id = (arguments.get("process_id", "") or "").strip()
        tail_bytes = _coerce_timeout(arguments.get("tail_bytes", 12000), default=12000, maximum=50000)
        if not process_id:
            return ToolResult("Missing 'process_id'", is_error=True)
        try:
            logs = self._executor.read_logs(process_id, tail_bytes=tail_bytes)
            if not logs:
                logs = "No logs captured yet."
            return ToolResult(logs)
        except Exception as e:
            return ToolResult(f"Log read error: {e}", is_error=True)


class ShellWaitTool(_ProcessReadTool):
    def __init__(self):
        super().__init__()
        self._executor = CommandExecutor()

    @property
    def name(self) -> str:
        return "shell_wait"

    @property
    def description(self) -> str:
        return "Wait for a background shell process to finish and return its final status plus recent logs."

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "process_id": {"type": "string", "description": "Background process id"},
                "timeout": {"type": "number", "description": "How long to wait in seconds (default 600, max 600)"},
            },
            "required": ["process_id"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        process_id = (arguments.get("process_id", "") or "").strip()
        timeout_sec = _coerce_timeout(arguments.get("timeout", 600))
        if not process_id:
            return ToolResult("Missing 'process_id'", is_error=True)
        try:
            snapshot = self._executor.wait(process_id, timeout_sec=timeout_sec)
            logs = self._executor.read_logs(process_id)
            parts = [snapshot.to_display_text()]
            if logs:
                parts.append(f"Recent logs:\n{logs}")
            return ToolResult("\n\n".join(parts))
        except Exception as e:
            return ToolResult(f"Wait error: {e}", is_error=True)


class ShellKillTool(BaseTool):
    def __init__(self):
        super().__init__()
        self._executor = CommandExecutor()

    @property
    def name(self) -> str:
        return "shell_kill"

    @property
    def description(self) -> str:
        return "Terminate a background shell process by process_id."

    @property
    def group(self) -> str:
        return "command"

    @property
    def category(self) -> str:
        return "command"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "process_id": {"type": "string", "description": "Background process id"},
            },
            "required": ["process_id"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        process_id = (arguments.get("process_id", "") or "").strip()
        if not process_id:
            return ToolResult("Missing 'process_id'", is_error=True)
        if not await context.ask_approval(f"Terminate background process {process_id}?"):
            return ToolResult("User denied process termination", is_error=True)
        try:
            snapshot = self._executor.kill(process_id)
            logs = self._executor.read_logs(process_id)
            parts = [snapshot.to_display_text()]
            if logs:
                parts.append(f"Recent logs:\n{logs}")
            return ToolResult("\n\n".join(parts))
        except Exception as e:
            return ToolResult(f"Kill error: {e}", is_error=True)
