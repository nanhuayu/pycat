"""Select an application entrypoint without importing an unused frontend."""
from __future__ import annotations

import sys

from pycat.core.hosts.askpass import ASKPASS_ARG, run_askpass
from pycat.core.hosts.python_worker import PYTHON_EXEC_WORKER_ARG, run_python_exec_worker
from pycat.core.tools.pty_child import PTY_CHILD_ARG, run_pty_child


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == PTY_CHILD_ARG:
        return run_pty_child(args[1:])
    if args and args[0] == ASKPASS_ARG:
        return run_askpass(args)
    gui = bool(args and args[0] == "--gui")
    if gui:
        args = args[1:]
    if args and args[0] == PYTHON_EXEC_WORKER_ARG:
        return run_python_exec_worker(args)
    if gui:
        # Optional frontend selection, not a second application/service layer.
        try:
            from pycat.gui.application import main as gui_main
        except ModuleNotFoundError as exc:
            if not str(exc.name).startswith("pycat.gui"):
                raise
            print("This headless build does not include the desktop frontend.", file=sys.stderr)
            return 2

        sys.argv = [sys.argv[0], *args]
        return gui_main()
    from pycat.cli.main import main as cli_main

    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
