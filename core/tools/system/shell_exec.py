from typing import Any, Dict

from core.tools.base import BaseTool, ToolContext, ToolResult
from core.tools.process import CommandExecutionRequest, CommandExecutor, is_dangerous_command


def _timeout(value: Any, default: int = 600, maximum: int = 600) -> int:
    try:
        return max(1, min(int(value or default), maximum))
    except Exception:
        return default


def _executor(context: ToolContext) -> CommandExecutor:
    return CommandExecutor(shell_config=getattr(getattr(context, "runtime", None), "shell_config", None))


class ExecuteCommandTool(BaseTool):
    @property
    def name(self) -> str:
        return "shell__run"

    @property
    def display_name(self) -> str:
        return "运行命令"

    @property
    def description(self) -> str:
        return "Run one bounded foreground shell command in the workspace and return its output."

    @property
    def category(self) -> str:
        return "execute"

    @property
    def risk(self) -> str:
        return "medium"

    def assess_risk(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        return "high" if is_dangerous_command(str(arguments.get("command") or "")) else "medium"

    def approval_message(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        return f"Run shell command?\n> {arguments.get('command') or ''}"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run."},
                "cwd": {"type": "string", "description": "Workspace-relative working directory; default '.'."},
                "timeout": {"type": "integer", "description": "Seconds before timeout; default and max 600."},
            },
            "required": ["command"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        command = str(arguments.get("command") or "").strip()
        if not command:
            return ToolResult("command is required.", is_error=True)
        try:
            cwd = context.resolve_path(str(arguments.get("cwd") or "."))
            timeout = _timeout(arguments.get("timeout"))
            result = _executor(context).execute(CommandExecutionRequest(command=command, cwd=cwd, timeout_sec=timeout))
            if result.timed_out:
                return ToolResult(f"Command timed out after {timeout}s; use shell__start for long-running work.", is_error=True)
            return ToolResult(result.to_display_text(cwd))
        except Exception as exc:
            return ToolResult(f"Execution error: {exc}", is_error=True)


class ShellStartTool(BaseTool):
    @property
    def name(self) -> str:
        return "shell__start"

    @property
    def display_name(self) -> str:
        return "启动后台命令"

    @property
    def description(self) -> str:
        return "Start one long-running shell command and return a process_id for shell__read."

    @property
    def category(self) -> str:
        return "execute"

    @property
    def risk(self) -> str:
        return "medium"

    def assess_risk(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        return "high" if is_dangerous_command(str(arguments.get("command") or "")) else "medium"

    def approval_message(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        return f"Start background shell command?\n> {arguments.get('command') or ''}"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to start."},
                "cwd": {"type": "string", "description": "Workspace-relative working directory; default '.'."},
            },
            "required": ["command"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        command = str(arguments.get("command") or "").strip()
        if not command:
            return ToolResult("command is required.", is_error=True)
        try:
            cwd = context.resolve_path(str(arguments.get("cwd") or "."))
            result = _executor(context).execute(CommandExecutionRequest(command=command, cwd=cwd, background=True))
            return ToolResult(result.to_display_text(cwd))
        except Exception as exc:
            return ToolResult(f"Execution error: {exc}", is_error=True)


class ShellReadTool(BaseTool):
    LOG_BYTES = 32 * 1024
    WAIT_SECONDS = 30

    @property
    def name(self) -> str:
        return "shell__read"

    @property
    def display_name(self) -> str:
        return "读取后台进程"

    @property
    def description(self) -> str:
        return "Read background process status and recent output, optionally waiting up to 30 seconds."

    @property
    def category(self) -> str:
        return "execute"

    @property
    def risk(self) -> str:
        return "low"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "process_id": {"type": "string", "description": "Process id returned by shell__start."},
                "wait": {"type": "boolean", "description": "Wait up to 30 seconds for completion; default false."},
            },
            "required": ["process_id"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        process_id = str(arguments.get("process_id") or "").strip()
        if not process_id:
            return ToolResult("process_id is required.", is_error=True)
        executor = _executor(context)
        try:
            if bool(arguments.get("wait")):
                try:
                    snapshot = executor.wait(process_id, timeout_sec=self.WAIT_SECONDS)
                except TimeoutError:
                    snapshot = executor.status(process_id)
            else:
                snapshot = executor.status(process_id)
            logs = executor.read_logs(process_id, tail_bytes=self.LOG_BYTES)
            body = snapshot.to_display_text()
            if logs:
                body += f"\noutput_tail:\n{logs}"
            return ToolResult(body)
        except Exception as exc:
            return ToolResult(f"Process read error: {exc}", is_error=True)


class ShellKillTool(BaseTool):
    @property
    def name(self) -> str:
        return "shell__kill"

    @property
    def display_name(self) -> str:
        return "终止后台进程"

    @property
    def description(self) -> str:
        return "Terminate one background process previously started by PyCat."

    @property
    def category(self) -> str:
        return "execute"

    @property
    def risk(self) -> str:
        return "medium"

    def approval_message(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        return f"Terminate background process {arguments.get('process_id') or ''}?"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {"process_id": {"type": "string", "description": "Process id returned by shell__start."}},
            "required": ["process_id"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        process_id = str(arguments.get("process_id") or "").strip()
        if not process_id:
            return ToolResult("process_id is required.", is_error=True)
        try:
            return ToolResult(_executor(context).kill(process_id).to_display_text())
        except Exception as exc:
            return ToolResult(f"Process termination error: {exc}", is_error=True)
