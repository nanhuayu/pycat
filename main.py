"""Developer entrypoint; installed users can run pycat-gui."""
from pycat.gui.application import main
from pycat.core.tools.system.python_worker import PYTHON_EXEC_WORKER_ARG, run_python_exec_worker
from pycat.core.hosts.askpass import ASKPASS_ARG, run_askpass
from pycat.core.tools.pty_child import PTY_CHILD_ARG, run_pty_child
import sys

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == PTY_CHILD_ARG:
        sys.exit(run_pty_child(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == ASKPASS_ARG:
        sys.exit(run_askpass())
    if len(sys.argv) > 1 and sys.argv[1] == PYTHON_EXEC_WORKER_ARG:
        sys.exit(run_python_exec_worker())
    raise SystemExit(main())
