import asyncio
import json
import os
import subprocess
from typing import Any, Dict

from pycat.core.hosts.python import resolve_python_runner, run_python_code
from pycat.core.tools.base import BaseTool, ToolContext, ToolResult
from pycat.core.tools.process import CommandExecutionRequest, decode_subprocess_output
from pycat.core.tools.system.shell_exec import _executor, _session_root


class PythonExecTool(BaseTool):
    @property
    def name(self) -> str:
        return "python__exec"

    @property
    def display_name(self) -> str:
        return "运行 Python"

    @property
    def description(self) -> str:
        return (
            "Execute Python on the workspace host without a sandbox; return exitCode, stdout and stderr. "
            "Each call is a new process with a maximum 60-second timeout. In packaged PyCat the local runner "
            "is a bundled worker, not an installable Python environment: sys.executable may not be a usable "
            "interpreter. Check imports first (PDF: import pymupdf); do not run pip through that executable. "
            "Use a verified external interpreter via shell__run for longer work or additional dependencies."
        )

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
                "timeout": {"type": "number", "minimum": 1, "maximum": 60,
                            "description": "Timeout seconds; default and maximum 60. Longer work requires shell__run."},
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
                run_python_code,
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
            ), is_error=proc.returncode != 0)
        except subprocess.TimeoutExpired:
            return ToolResult(f"Python execution timed out after {timeout_sec:.0f}s", is_error=True)
        except Exception as e:
            return ToolResult(f"Python execution error: {e}", is_error=True)
