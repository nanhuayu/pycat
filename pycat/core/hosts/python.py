"""Local Python execution shared by tools and isolated document parsing."""
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from pycat.core.hosts.python_worker import PYTHON_EXEC_WORKER_ARG

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


def resolve_python_runner(*, use_configured: bool = True) -> list[str] | None:
    """Return the command prefix used to execute temp Python scripts.

    Source mode uses the active Python interpreter. Packaged Nuitka mode uses
    the current PyCat executable in a hidden worker mode, so python__exec works
    without requiring a separately installed python.exe.
    """
    configured = os.environ.get("PYCAT_PYTHON") or os.environ.get("PYTHON")
    if configured and use_configured:
        path = Path(configured).expanduser()
        if path.exists() and path.is_file():
            return _build_python_command(str(path))

    if not _is_nuitka_runtime():
        return _build_python_command(_current_executable())

    return [_current_executable(), PYTHON_EXEC_WORKER_ARG]


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


def run_python_code(
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
