"""Hidden Python worker shared by tools and bounded document parsing."""
from __future__ import annotations

import os
import runpy
import sys

PYTHON_EXEC_WORKER_ARG = "--pycat-python-exec-worker"


def run_python_exec_worker(argv: list[str] | None = None) -> int:
    """Execute a temp Python script and return a process exit code."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != PYTHON_EXEC_WORKER_ARG:
        return -1
    if len(args) != 2 or not args[1]:
        sys.stderr.write("Missing python__exec worker script path.\n")
        return 2

    script_path = os.path.abspath(args[1])
    if not os.path.isfile(script_path):
        sys.stderr.write(f"python__exec worker script not found: {script_path}\n")
        return 2

    sys.argv = [script_path]
    runpy.run_path(script_path, run_name="__main__")
    return 0
