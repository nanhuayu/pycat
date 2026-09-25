import asyncio
import subprocess
import time
from pathlib import Path
from typing import Any, Dict

from pycat.core.tools.base import BaseTool, ToolContext, ToolResult
from pycat.core.tools.process import CommandExecutionRequest, CommandExecutor, is_dangerous_command
from pycat.models.contracts.config import ShellConfig
from pycat.models.session_paths import resolve_session_root

_FOREGROUND_WAIT_MAX = 600


def _shell_config(context: ToolContext) -> ShellConfig:
    return getattr(getattr(context, "runtime", None), "shell_config", None) or ShellConfig()


def _wait_seconds(value: Any, default: int, *, allow_zero: bool = False) -> int:
    """Clamp a wait_seconds argument to the configured knob bound (0 = immediate background)."""
    try:
        parsed = int(value if value is not None else default)
    except Exception:
        return default
    if allow_zero and parsed <= 0:
        return 0
    return max(1, min(parsed, _FOREGROUND_WAIT_MAX))


def _conversation_id(context: ToolContext) -> str:
    return str(getattr(getattr(context, "conversation", None), "id", "") or "")


def _session_root(context: ToolContext) -> Path | None:
    """Canonical session root for process logs; None when no conversation is bound."""
    conversation_id = _conversation_id(context)
    if not conversation_id:
        return None
    work_dir = str(
        getattr(getattr(context, "conversation", None), "work_dir", "")
        or getattr(context, "work_dir", "")
        or ""
    ).strip()
    return resolve_session_root(work_dir, conversation_id, data_dir=context.data_dir)


def _executor(context: ToolContext) -> CommandExecutor:
    manager = getattr(getattr(context, "runtime", None), "process_manager", None)
    if manager is None:
        raise RuntimeError("background process manager is unavailable in this tool context")
    return CommandExecutor(
        manager,
        shell_config=_shell_config(context),
        conversation_id=_conversation_id(context),
    )


class ExecuteCommandTool(BaseTool):
    @property
    def name(self) -> str:
        return "shell__run"

    @property
    def display_name(self) -> str:
        return "运行命令"

    @property
    def description(self) -> str:
        return (
            "Run a shell command OR a program with literal argv and UTF-8 stdin, with a bounded wait. "
            "Use program/argv/stdin for multiline scripts and paths with spaces. wait_seconds=0 returns immediately. "
            "When the wait expires the command keeps running in background and a process_id "
            "is returned for shell__read / shell__kill. Set interactive=true to create a real terminal "
            "for later shell__write input; omit command/program to open a persistent default shell."
        )

    @property
    def category(self) -> str:
        return "execute"

    @property
    def risk(self) -> str:
        return "medium"

    def assess_risk(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        text = " ".join(str(arguments.get(key) or "") for key in ("command", "program", "argv", "stdin"))
        return "high" if is_dangerous_command(text) else "medium"

    def approval_message(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        command = arguments.get("command") or subprocess.list2cmdline([str(arguments.get("program") or ""), *arguments.get("argv", [])])
        script = f"\nstdin:\n{arguments['stdin']}" if arguments.get("stdin") else ""
        return f"Run command?\n> {command}{script}"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run."},
                "program": {"type": "string", "description": "Executable path or name; mutually exclusive with command."},
                "argv": {"type": "array", "items": {"type": "string"}, "description": "Literal arguments for program; no shell quoting."},
                "stdin": {"type": "string", "description": "UTF-8 input, for example a multiline script passed to python - or node -."},
                "interactive": {"type": "boolean", "description": "Create a terminal with persistent input (shell__write). Default false."},
                "cwd": {"type": "string", "description": "Workspace-relative working directory; default '.'."},
                "wait_seconds": {
                    "type": "integer",
                    "description": (
                        "Seconds to wait for completion before returning a background process_id "
                        "(the process is NOT killed); 0 = return immediately, "
                        "default from shell settings (10), max 600."
                    ),
                },
            },
            "required": [],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        command = str(arguments.get("command") or "").strip()
        program = str(arguments.get("program") or "").strip()
        argv = arguments.get("argv", [])
        stdin = arguments.get("stdin")
        interactive = bool(arguments.get("interactive", False))
        if (command and program) or (not interactive and not (command or program)):
            return ToolResult("Provide exactly one of command or program, or interactive=true for a persistent shell.", is_error=True)
        if interactive and stdin is not None:
            return ToolResult("Interactive input must use shell__write after startup.", is_error=True)
        if not isinstance(argv, list) or any(not isinstance(arg, str) for arg in argv) or (argv and not program):
            return ToolResult("argv must be an array of strings used with program.", is_error=True)
        if stdin is not None and not isinstance(stdin, str):
            return ToolResult("stdin must be a UTF-8 string.", is_error=True)
        try:
            cwd = context.resolve_workspace_path(str(arguments.get("cwd") or "."))
            wait_seconds = _wait_seconds(
                arguments.get("wait_seconds"),
                _shell_config(context).wait_seconds,
                allow_zero=True,
            )
            executor = _executor(context)
            request = CommandExecutionRequest(
                command=command or (subprocess.list2cmdline([program, *argv]) if program else ""),
                cwd=cwd,
                timeout_sec=wait_seconds,
                background=interactive or wait_seconds == 0,
                conversation_id=_conversation_id(context),
                session_root=_session_root(context),
                program=program, argv=tuple(argv), stdin=stdin, remote=context.files,
                interactive=interactive,
                controller=context.runtime.terminal_controller,
            )
            result = await asyncio.to_thread(executor.execute, request)
            # Timing out is a normal outcome: the process survived in background
            # and the result carries its process_id handle.
            return ToolResult(result.to_display_text(cwd),
                is_error=bool(result.error) or (not result.running and result.exit_code not in (None, 0)),
                metadata={"process_id": result.process_id, "pid": result.pid, "cwd": str(cwd),
                          "backend": result.backend, "running": result.running, "exit_code": result.exit_code,
                          "interactive": interactive})
        except Exception as exc:
            return ToolResult(f"Execution error: {exc}", is_error=True)


class ShellReadTool(BaseTool):
    LOG_BYTES = 32 * 1024

    @property
    def name(self) -> str:
        return "shell__read"

    @property
    def display_name(self) -> str:
        return "读取后台进程"

    @property
    def description(self) -> str:
        return (
            "Read background process status and log output incrementally: pass cursor "
            "(byte offset, default 0) and continue from next_cursor while has_more is true. "
            "Optionally polls until completion for up to wait_seconds (bounded by shell settings)."
        )

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
                "process_id": {"type": "string", "description": "Process id returned by shell__run."},
                "cursor": {
                    "type": "integer",
                    "description": "Byte offset to read from; use the previous next_cursor for incremental reads. Default 0.",
                },
                "wait_seconds": {
                    "type": "integer",
                    "description": "Seconds to keep polling for completion; default 0 (single snapshot), max from shell settings (10).",
                },
            },
            "required": ["process_id"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        process_id = str(arguments.get("process_id") or "").strip()
        if not process_id:
            return ToolResult("process_id is required.", is_error=True)
        executor = _executor(context)
        wait_seconds = _wait_seconds(arguments.get("wait_seconds"), 0, allow_zero=True)
        wait_seconds = min(wait_seconds, _shell_config(context).wait_seconds)
        try:
            cursor = max(0, int(arguments.get("cursor") or 0))
        except Exception:
            cursor = 0
        try:
            snapshot = executor.status(process_id)
            if wait_seconds > 0:
                deadline = time.monotonic() + wait_seconds
                while snapshot.running and time.monotonic() < deadline:
                    await asyncio.sleep(0.5)
                    snapshot = executor.status(process_id)
            chunk = executor.read(process_id, cursor=cursor, max_bytes=self.LOG_BYTES)
            return ToolResult(chunk.to_display_text(), is_error=bool(chunk.snapshot.error) or (
                not chunk.snapshot.running and chunk.snapshot.exit_code not in (None, 0)),
                metadata={"process_id": process_id, "next_cursor": chunk.next_cursor,
                          "has_more": chunk.has_more, "running": chunk.snapshot.running})
        except Exception as exc:
            return ToolResult(f"Process read error: {exc}", is_error=True)


class ShellListTool(BaseTool):
    @property
    def name(self) -> str:
        return "shell__list"

    @property
    def display_name(self) -> str:
        return "列出后台进程"

    @property
    def description(self) -> str:
        return "List background processes started by PyCat with status, elapsed time and log size."

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
                "all": {
                    "type": "boolean",
                    "description": "Include exited processes; default false (running only).",
                },
            },
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            snapshots = _executor(context).list(include_exited=bool(arguments.get("all")))
            if not snapshots:
                return ToolResult("No background processes.")
            return ToolResult("\n\n".join(snapshot.to_display_text() for snapshot in snapshots))
        except Exception as exc:
            return ToolResult(f"Process list error: {exc}", is_error=True)


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
            "properties": {"process_id": {"type": "string", "description": "Process id returned by shell__run."}},
            "required": ["process_id"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        process_id = str(arguments.get("process_id") or "").strip()
        if not process_id:
            return ToolResult("process_id is required.", is_error=True)
        try:
            snapshot = await asyncio.to_thread(_executor(context).kill, process_id)
            return ToolResult(snapshot.to_display_text())
        except Exception as exc:
            return ToolResult(f"Process termination error: {exc}", is_error=True)


class ShellWriteTool(BaseTool):
    """Agent input shares permissions/receipts and respects a human takeover."""

    name = "shell__write"
    display_name = "输入终端"
    category = "execute"
    risk = "medium"
    description = (
        "Send text to an interactive process created by shell__run. Include \\r to submit a line; "
        "use \\u0003 for Ctrl+C; EOF depends on the target program. Input is rejected while the user controls the terminal. "
        "Delivery is not command completion; inspect shell__read. Never automatically retry uncertain input."
    )
    input_schema = {
        "type": "object", "properties": {
            "process_id": {"type": "string"},
            "text": {"type": "string", "description": "Literal terminal input, at most 64 KiB."},
        }, "required": ["process_id", "text"], "additionalProperties": False,
    }

    def assess_risk(self, arguments, context):
        return "high" if is_dangerous_command(str(arguments.get("text", ""))) else "medium"

    def approval_message(self, arguments, context):
        return f"Send input to terminal {arguments.get('process_id', '')}?\n{arguments.get('text', '')}"

    async def execute(self, arguments, context):
        try:
            manager = context.runtime.process_manager
            count = await asyncio.to_thread(manager.write, str(arguments["process_id"]), arguments["text"],
                conversation_id=_conversation_id(context), controller="agent")
            return ToolResult(f"Terminal accepted {count} bytes. Use shell__read to inspect output.",
                              metadata={"process_id": str(arguments["process_id"]), "accepted_bytes": count})
        except Exception as exc:
            return ToolResult(f"Terminal input error: {exc}", is_error=True)
