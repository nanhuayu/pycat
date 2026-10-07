"""Acquire a POSIX controlling terminal in a fresh, single-threaded process."""
from __future__ import annotations

import os
import sys

PTY_CHILD_ARG = "--pycat-pty-child"


def run_pty_child(args: list[str]) -> int:
    if os.name == "nt" or not args:
        return 2
    import fcntl
    import termios

    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.execvpe(args[0], args, os.environ)
    return 127


if __name__ == "__main__":
    raise SystemExit(run_pty_child(sys.argv[1:]))
