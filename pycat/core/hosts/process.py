"""Popen-shaped remote process handle owned by BackgroundProcessManager."""
from __future__ import annotations

import base64
import subprocess
import threading


class SshProcess:
    def __init__(self, files, request, log):
        self.connection = files.connection
        result = files.call("process_start", request.cwd, write=True, program=request.program,
            argv=list(request.argv), command=request.command, stdin=request.stdin)
        self.pid = result["pid"]
        self.remote_id = result["process_id"]
        self.returncode = None
        self.error = ""
        self._done = threading.Event()
        self._log = log
        threading.Thread(target=self._drain, daemon=True, name=f"ssh-process-{self.pid}").start()

    def _drain(self):
        cursor = 0
        try:
            while True:
                result = self.connection.call("process_read", process_id=self.remote_id, cursor=cursor)
                if result["cursor"] > cursor:
                    self._log.write(b"\n[Remote output exceeded buffer; earlier bytes unavailable]\n")
                data = base64.b64decode(result["data"])
                self._log.write(data)
                self._log.flush()
                cursor = result["next_cursor"]
                if result["exit_code"] is not None and result["drained"] and not data:
                    self.returncode = result["exit_code"]
                    try:
                        self.connection.call("process_release", process_id=self.remote_id)
                    except (OSError, ValueError):
                        pass  # Exit/output are confirmed; connection cleanup owns any leftover handle.
                    return
                if not data:
                    self._done.wait(0.15)
        except (OSError, ValueError, ConnectionError) as exc:
            self.error = f"SSH process result unknown: {exc}"
            self._log.write(("\n" + self.error + "\n").encode("utf-8"))
            self._log.flush()
            self.returncode = -1
        finally:
            self._done.set()

    def poll(self):
        return self.returncode if self._done.is_set() else None

    def wait(self, timeout=None):
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired("SSH process", timeout)
        return self.returncode

    def terminate(self):
        if self.returncode is None:
            self.connection.call("process_kill", process_id=self.remote_id)

    kill = terminate
