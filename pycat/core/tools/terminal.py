"""Popen-shaped native terminal handle; the process manager owns its lifetime."""
from __future__ import annotations

import os
import select
import signal
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from pycat.core.tools.pty_child import PTY_CHILD_ARG

if os.name == "nt":
    try:
        from winpty import PTY, Backend
    except ImportError:
        PTY = None
else:
    import fcntl
    import pty
    import termios


class TerminalProcess:
    """Drain continuously to the canonical log, independently of visible views."""

    def __init__(self, argv, *, cwd, env, log, columns=100, rows=30):
        self.error = ""
        self.returncode = None
        self._done = threading.Event()
        self._io_lock = threading.Lock()
        self._log = log
        self._pty = None
        self._master = None
        if os.name == "nt":
            if PTY is None:
                raise RuntimeError("Interactive Shell requires pywinpty on Windows")
            self._pty = PTY(columns, rows, backend=Backend.ConPTY)
            environment = "\0".join(f"{k}={v}" for k, v in sorted(env.items())) + "\0"
            try:
                self._pty.spawn(argv[0], cmdline=subprocess.list2cmdline(argv[1:]), cwd=str(cwd), env=environment)
                self.pid = self._pty.pid
            except BaseException:
                self._pty = None
                raise
        else:
            master, slave = pty.openpty()
            self._master = master
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
            # No Python preexec_fn/fork in the threaded GUI. The fresh child
            # acquires the controlling tty, then execs the requested program.
            child = ([sys.executable, PTY_CHILD_ARG] if getattr(sys, "frozen", False) or "__compiled__" in globals()
                     else [sys.executable, str(Path(__file__).with_name("pty_child.py"))])
            try:
                self._process = subprocess.Popen(child + list(argv), cwd=str(cwd), env=env,
                    stdin=slave, stdout=slave, stderr=slave, start_new_session=True, close_fds=True)
                self.pid = self._process.pid
            except BaseException:
                os.close(master)
                raise
            finally:
                os.close(slave)
        self._reader = threading.Thread(target=self._drain, name=f"terminal-{self.pid}", daemon=True)
        self._reader.start()

    def _drain(self):
        try:
            while True:
                if os.name == "nt":
                    try:
                        data = self._pty.read(blocking=False).encode("utf-8")
                    except EOFError:
                        break
                    if not data and not self._pty.isalive():
                        break
                else:
                    if not select.select([self._master], [], [], 0.03)[0]:
                        if self._process.poll() is not None:
                            break
                        continue
                    try:
                        data = os.read(self._master, 32768)
                    except OSError as exc:
                        if exc.errno == 5:  # Linux PTY EOF; macOS returns b''.
                            break
                        raise
                    if not data:
                        break
                if data:
                    self._log.write(data)
                    self._log.flush()
                else:
                    time.sleep(0.01)
            self.returncode = self._pty.get_exitstatus() if os.name == "nt" else self._process.wait()
            if self.returncode is None:
                self.error = "Terminal closed without an exit status"
                self.returncode = -1
        except Exception as exc:
            self.error = f"Terminal transport failed: {exc}"
            self.returncode = -1
            self.terminate()
        finally:
            with self._io_lock:
                if self._master is not None:
                    os.close(self._master)
                    self._master = None
                self._pty = None
            self._done.set()

    def write(self, text: str):
        with self._io_lock:
            if self._done.is_set() or (self._pty is None and self._master is None):
                raise ValueError("Terminal has exited")
            if os.name == "nt":
                return self._pty.write(text)
            data = text.encode("utf-8")
            offset = 0
            deadline = time.monotonic() + 5
            while offset < len(data):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Terminal input delivery incomplete; do not automatically resend")
                if select.select([], [self._master], [], 0.1)[1]:
                    offset += os.write(self._master, data[offset:offset + 1024])
            return offset

    def resize(self, columns: int, rows: int):
        with self._io_lock:
            if os.name == "nt" and self._pty is not None:
                self._pty.set_size(columns, rows)
            elif self._master is not None:
                fcntl.ioctl(self._master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))

    def poll(self):
        return self.returncode if self._done.is_set() else None

    def wait(self, timeout=None):
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired("terminal", timeout)
        return self.returncode

    def terminate(self):
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.pid)], capture_output=True,
                           timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            try:
                # The shell and its foreground jobs can use different groups.
                if self._master is not None:
                    foreground = os.tcgetpgrp(self._master)
                    if foreground > 0 and foreground != self.pid:
                        os.killpg(foreground, signal.SIGKILL)
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    kill = terminate
