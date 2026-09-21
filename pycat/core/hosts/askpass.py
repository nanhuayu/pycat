"""Ephemeral authenticated loopback bridge for OpenSSH prompts. Stdlib only."""
from __future__ import annotations

import hmac
import ctypes
from ctypes import wintypes
import json
import os
import re
from pathlib import Path
import secrets
import shlex
import socket
import subprocess
import sys
import tempfile
import threading

ASKPASS_ARG = "--pycat-ssh-askpass"


def run_askpass(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == ASKPASS_ARG:
        args = args[1:]
    try:
        port = int(os.environ["PYCAT_SSH_AUTH_PORT"])
        token = os.environ["PYCAT_SSH_AUTH_TOKEN"]
        prompt = " ".join(args)[:8192]
        hint = os.environ.get("SSH_ASKPASS_PROMPT", "")
        if hint == "none":
            return 0
        with socket.create_connection(("127.0.0.1", port), timeout=130) as connection:
            connection.sendall(json.dumps({"token": token, "prompt": prompt, "confirm": hint == "confirm"}).encode() + b"\n")
            with connection.makefile("rb") as stream:
                reply = json.loads(stream.readline(16384))
        if reply.get("answer") is None:
            return 1
        # OpenSSH consumes this private pipe, never application logs.
        answer = str(reply["answer"]).replace("\n", "").replace("\r", "") + "\n"
        if sys.stdout is not None:
            sys.stdout.write(answer)
            sys.stdout.flush()
        elif os.name == "nt":
            # A windowed executable still inherits SSH's private stdout pipe.
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetStdHandle.restype = wintypes.HANDLE
            kernel.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
            data, written = answer.encode("utf-8"), wintypes.DWORD()
            if not kernel.WriteFile(kernel.GetStdHandle(-11), data, len(data), ctypes.byref(written), None):
                return 1
        else:
            return 1
        return 0
    except (KeyError, OSError, ValueError):
        return 1


class AskpassBridge:
    def __init__(self, prompt=None, *, password: str | None = None):
        self.prompt = prompt
        self._password = password
        self.token = secrets.token_urlsafe(32)
        self._closed = threading.Event()
        self._socket = socket.socket()
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(2)
        self._socket.settimeout(0.25)
        self._temp = tempfile.TemporaryDirectory(prefix="pycat-ssh-auth-")
        self._thread = threading.Thread(target=self._serve, daemon=True, name="ssh-auth")
        self._thread.start()

    def environment(self):
        compiled = "__compiled__" in globals() or getattr(sys, "frozen", False)
        command = [os.path.abspath(sys.orig_argv[0]), ASKPASS_ARG] if compiled else [sys.executable, str(Path(__file__).resolve())]
        if os.name == "nt":
            # Windows OpenSSH spawn accepts an executable followed by literal args.
            launcher = subprocess.list2cmdline(command)
        else:
            path = Path(self._temp.name) / "askpass"
            path.write_text("#!/bin/sh\nexec " + shlex.join(command) + ' "$@"\n', encoding="utf-8")
            path.chmod(0o700)
            launcher = str(path)
        return {"SSH_ASKPASS": launcher, "SSH_ASKPASS_REQUIRE": "force",
                "PYCAT_SSH_AUTH_PORT": str(self._socket.getsockname()[1]), "PYCAT_SSH_AUTH_TOKEN": self.token}

    def _serve(self):
        while not self._closed.is_set():
            try:
                connection, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with connection:
                connection.settimeout(5)
                try:
                    with connection.makefile("rb") as stream:
                        request = json.loads(stream.readline(16384))
                    if not hmac.compare_digest(str(request.get("token", "")), self.token):
                        continue
                    answer = self._answer(str(request.get("prompt", ""))[:8192], bool(request.get("confirm")))
                    if self._closed.is_set():
                        answer = None
                    connection.sendall(json.dumps({"answer": answer}).encode() + b"\n")
                    answer = None
                except (OSError, ValueError):
                    continue

    def _answer(self, text, confirm):
        if self._closed.is_set():
            return None
        # Never use a login password for host trust, key passphrases or OTP challenges.
        if not confirm and self._password is not None and re.fullmatch(
            r"(?:\S+@\S+'s |\([^\r\n]+\) )?password:\s*", text.strip(), re.IGNORECASE
        ):
            answer, self._password = self._password, None
            return answer
        prompt = self.prompt
        return prompt(text, confirm) if prompt is not None else None

    def close(self):
        self._closed.set()
        self._password = None
        self.prompt = None
        self._socket.close()
        self._temp.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


if __name__ == "__main__":
    raise SystemExit(run_askpass())
