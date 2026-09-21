import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

from pycat.core.tools.base import BaseTool, ToolContext, ToolResult
from pycat.core.tools.process import decode_subprocess_output
from pycat.core.tools.process import CommandExecutionRequest
from pycat.core.tools.system.shell_exec import _executor, _session_root
from pycat.core.tools.system.python_worker import PYTHON_EXEC_WORKER_ARG


logger = logging.getLogger(__name__)


def _is_nuitka_runtime() -> bool:
    return "__compiled__" in globals()


def _current_executable() -> str:
    if _is_nuitka_runtime():
        # Nuitka may leave sys.executable pointing to a nonexistent python.exe
        # until a multiprocessing/AnyIO hook runs. runpy also replaces argv[0].
        # orig_argv retains our real entry, including in nested worker calls.
        entry = sys.orig_argv[0]
        return os.path.abspath(shutil.which(entry) or entry)
    return sys.executable


def resolve_python_runner() -> list[str] | None:
    """Return the command prefix used to execute temp Python scripts.

    Source mode uses the active Python interpreter. Packaged Nuitka mode uses
    the current PyCat executable in a hidden worker mode, so python__exec works
    without requiring a separately installed python.exe.
    """
    configured = os.environ.get("PYCAT_PYTHON") or os.environ.get("PYTHON")
    if configured:
        path = Path(configured).expanduser()
        if path.exists() and path.is_file():
            return _build_python_command(str(path))

    if not _is_nuitka_runtime():
        return _build_python_command(_current_executable())

    return [_current_executable(), PYTHON_EXEC_WORKER_ARG]


def resolve_python_interpreter() -> str | None:
    """Compatibility helper for settings/tests that need the interpreter path."""
    configured = os.environ.get("PYCAT_PYTHON") or os.environ.get("PYTHON")
    if configured:
        path = Path(configured).expanduser()
        if path.exists() and path.is_file():
            return str(path)

    if not _is_nuitka_runtime():
        return _current_executable()

    return _current_executable()


def _build_python_command(interpreter: str) -> list[str]:
    if os.name == "nt" and Path(interpreter).name.lower() == "py.exe":
        return [interpreter, "-3"]
    return [interpreter]


def _subprocess_stdio_options() -> dict[str, Any]:
    """Return stdio options safe for GUI-subsystem Nuitka executables.

    Packaged PyCat may run without valid inherited standard handles. Explicitly
    providing stdin avoids Windows ``[WinError 6] invalid handle`` when spawning
    the hidden python__exec worker from the GUI process.
    """
    options: dict[str, Any] = {"stdin": subprocess.DEVNULL}
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    return options


def _run_python_code(
    *,
    code: str,
    python_runner: list[str],
    cwd: Path,
    timeout_sec: float,
    env: dict[str, str],
) -> subprocess.CompletedProcess:
    script_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            prefix="pycat_exec_",
            encoding="utf-8",
            delete=False,
        ) as handle:
            script_path = handle.name
            handle.write(code)
            if not code.endswith("\n"):
                handle.write("\n")

        return subprocess.run(
            [*python_runner, script_path],
            cwd=str(cwd),
            capture_output=True,
            text=False,
            timeout=timeout_sec,
            env=env,
            **_subprocess_stdio_options(),
        )
    finally:
        if script_path:
            try:
                os.unlink(script_path)
            except FileNotFoundError:
                pass
            except Exception as exc:
                logger.debug("Failed to remove python exec temp file %s: %s", script_path, exc)


class PythonExecTool(BaseTool):
    @property
    def name(self) -> str:
        return "python__exec"

    @property
    def display_name(self) -> str:
        return "运行 Python"

    @property
    def description(self) -> str:
        return "Execute supplied Python code on the workspace host without a sandbox and return stdout and stderr."

    @property
    def category(self) -> str:
        return "execute"

    @property
    def risk(self) -> str:
        return "high"

    def approval_message(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        preview = str(arguments.get("code") or "")[:160]
        return f"Run unsandboxed Python code?\n{preview}"


    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute"},
                "timeout": {"type": "number", "description": "Timeout seconds (default: 60)"},
                "cwd": {"type": "string", "description": "Workspace-relative working directory (default: '.')"},
            },
            "required": ["code"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        code = arguments.get("code", "")
        timeout_sec = float(arguments.get("timeout", 60) or 60)
        cwd_str = arguments.get("cwd", ".")
        
        if not code or not code.strip():
            return ToolResult("Missing 'code'", is_error=True)
            
        timeout_sec = max(1.0, min(timeout_sec, 60.0))
        
        try:
            cwd_path = context.resolve_workspace_path(cwd_str)
        except Exception as e:
            return ToolResult(f"Invalid cwd: {e}", is_error=True)

        try:
            if context.files:
                executor = _executor(context)
                program = context.files.connection.info["executable"]
                request = CommandExecutionRequest(command=f"{program} -c <code>", cwd=cwd_path,
                    program=program, argv=("-c", str(code)), timeout_sec=timeout_sec,
                    session_root=_session_root(context), remote=context.files)
                result = await asyncio.to_thread(executor.execute, request)
                if result.running:
                    await asyncio.to_thread(executor.kill, result.process_id)
                    return ToolResult(f"Python execution timed out after {timeout_sec:.0f}s", is_error=True)
                return ToolResult(json.dumps({"exitCode": result.exit_code, "stdout": result.stdout, "stderr": ""}, ensure_ascii=False),
                    is_error=result.exit_code not in (0, None))
            env = dict(os.environ or {})
            # Prefer UTF-8 to reduce mojibake across Windows terminals.
            env.setdefault("PYTHONUTF8", "1")
            env.setdefault("PYTHONIOENCODING", "utf-8")
            python_runner = resolve_python_runner()
            if not python_runner:
                return ToolResult(
                    "Python execution runner not found.",
                    is_error=True,
                )

            proc = await asyncio.to_thread(
                _run_python_code,
                code=str(code),
                python_runner=python_runner,
                cwd=cwd_path,
                timeout_sec=timeout_sec,
                env=env,
            )
            return ToolResult(json.dumps(
                {
                    "exitCode": proc.returncode,
                    "stdout": decode_subprocess_output(proc.stdout).strip(),
                    "stderr": decode_subprocess_output(proc.stderr).strip(),
                },
                ensure_ascii=False,
                indent=2,
            ))
        except subprocess.TimeoutExpired:
            return ToolResult(f"Python execution timed out after {timeout_sec:.0f}s", is_error=True)
        except Exception as e:
            return ToolResult(f"Python execution error: {e}", is_error=True)
