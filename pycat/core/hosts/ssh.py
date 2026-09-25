"""System OpenSSH transport with bounded frames and no command replay."""
from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import threading
from contextlib import nullcontext
from pathlib import Path

from pycat.core.hosts import remote_helper
from pycat.core.hosts.askpass import AskpassBridge
from pycat.models.workspace import WorkspaceLocation


class SshConnection:
    def __init__(self, process: subprocess.Popen):
        self.process = process
        self._lock = threading.Lock()
        self._responses = queue.Queue(maxsize=2)
        self._closed = threading.Event()
        self._counter = 0
        self._stderr = bytearray()
        self._reader = threading.Thread(target=self._read, daemon=True, name="ssh-responses")
        self._reader.start()
        self._error_reader = threading.Thread(target=self._drain_errors, daemon=True, name="ssh-errors")
        self._error_reader.start()

    @classmethod
    def from_process(cls, process):
        """Attach an explicitly owned stdio helper (also used by protocol tests)."""
        return cls(process)

    @property
    def closed(self):
        return self._closed.is_set()

    @classmethod
    def connect(cls, host: str, *, port: int | None = None, password=None,
                prompt=None, cancel_event=None) -> SshConnection:
        host = WorkspaceLocation.ssh(host, "/", port=port).host
        executable = shutil.which("ssh")
        if not executable and os.name == "nt":
            candidate = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32/OpenSSH/ssh.exe"
            executable = str(candidate) if candidate.is_file() else None
        if not executable:
            raise FileNotFoundError("未找到 OpenSSH 客户端。Windows 请安装“可选功能 → OpenSSH 客户端”；Ubuntu/Debian：sudo apt install openssh-client")
        source = Path(__file__).with_name("remote_helper.py").read_bytes()
        digest = hashlib.sha256(source).hexdigest()
        # Fixed bootstrap, no user paths/code in a remote shell command.
        # Unbuffered bootstrap must not read ahead into the first protocol frame.
        bootstrap = ("import os,sys,hashlib\n"
            "if sys.version_info<(3,11): sys.exit(78)\n"
            "if os.name=='nt':\n"
            " import msvcrt; msvcrt.setmode(0,os.O_BINARY)\n"
            "sys.stdout.write('{\"bootstrap\":true}\\n'); sys.stdout.flush()\n"
            "i=os.fdopen(0,'rb',buffering=0,closefd=False)\n"
            "n=int(i.readline(20)); assert 0<n<1048576\n"
            "s=bytearray()\n"
            "while len(s)<n:\n"
            " c=i.read(n-len(s)); assert c; s.extend(c)\n"
            "exec(compile(bytes(s),'<pycat-workspace>','exec'),{'__name__':'__main__','SOURCE_DIGEST':hashlib.sha256(s).hexdigest()})")
        args = [executable, "-T", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2",
                "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes"]
        interactive = prompt is not None or password is not None
        args.extend(["-o", f"BatchMode={'no' if interactive else 'yes'}",
                     "-o", f"StrictHostKeyChecking={'ask' if interactive else 'yes'}"])
        if password is not None:
            args.extend(["-o", "PreferredAuthentications=password,keyboard-interactive,publickey"])
        if port is not None:
            args.extend(["-p", str(port)])
        # Hex-encoded fixed code needs no shell-specific quoting. The same command
        # is understood by POSIX shells, cmd.exe and PowerShell SSH default shells.
        code = f'"exec(bytes.fromhex(\'{bootstrap.encode().hex()}\'))"'
        for interpreter in ("python3", "python", "py -3"):
            if cancel_event is not None and cancel_event.is_set():
                raise ConnectionError("SSH 连接已取消")
            command = f"{interpreter} -u -c {code}"
            with AskpassBridge(prompt, password=password) if interactive else nullcontext() as bridge:
                env = {**os.environ, **(bridge.environment() if bridge else {})}
                process = subprocess.Popen([*args, host, command], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                connection = cls._handshake(process, source, digest, interactive, cancel_event)
                if connection is not None:
                    return connection
        raise ConnectionError("SSH 已连接，但未找到 Python 3.11+。请在远端安装 Python 并加入 SSH 会话的 PATH（python3、python 或 py -3）。")

    @classmethod
    def _handshake(cls, process, source, digest, interactive, cancel_event):
        connection = cls(process)
        started = False
        ready = threading.Event()
        def watch_cancel():
            while not ready.wait(0.1):
                if cancel_event is not None and cancel_event.is_set():
                    process.kill()
                    return
        if cancel_event is not None:
            threading.Thread(target=watch_cancel, daemon=True, name="ssh-connect-cancel").start()
        try:
            # Send no source or RPC until Python acknowledges startup. PowerShell
            # normalizes missing executable/native failure exit codes to 1.
            # This also bounds authentication before a potentially blocking write.
            reply = connection._responses.get(timeout=150 if interactive else 20)
            if reply != {"bootstrap": True}:
                raise ConnectionError("远端 Python 启动未完成")
            started = True
            process.stdin.write(f"{len(source)}\n".encode() + source)
            process.stdin.flush()
            hello = connection.call("hello", timeout=150 if interactive else 20)
            if hello["protocol"] != remote_helper.PROTOCOL or hello["digest"] != digest:
                raise ConnectionError("SSH helper protocol/digest mismatch")
            if hello["platform"] not in ("linux", "win32") or tuple(hello["python"]) < (3, 11):
                raise ValueError("远端需要 Windows 或 Linux，以及 Python 3.11+")
            connection.info = hello
            threading.Thread(target=connection._heartbeat, daemon=True, name="ssh-heartbeat").start()
            return connection
        except (OSError, queue.Empty) as exc:
            connection.close()
            if cancel_event is not None and cancel_event.is_set():
                raise ConnectionError("SSH 连接已取消") from None
            # Only pre-source interpreter discovery may retry. OpenSSH auth and
            # transport errors (255), and all subsequent operations, never replay.
            if not started and process.returncode in (1, 127, 9009, 78):
                return None
            raise connection._failure(connecting=True, cause=exc) from None
        except BaseException:
            connection.close()
            raise
        finally:
            ready.set()

    def _read(self):
        try:
            while not self._closed.is_set():
                line = self.process.stdout.readline(remote_helper.MAX_FRAME + 1)
                if not line or len(line) > remote_helper.MAX_FRAME:
                    break
                self._responses.put_nowait(json.loads(line))
        except (OSError, ValueError, queue.Full):
            pass
        finally:
            try:
                self._responses.put_nowait(None)
            except queue.Full:
                pass

    def _drain_errors(self):
        try:
            while data := self.process.stderr.read1(4096):
                self._stderr.extend(data)
                del self._stderr[:-8192]
        except (OSError, ValueError):
            pass

    def _failure(self, *, connecting=False, cause=None):
        details = self._stderr.decode("utf-8", errors="replace").strip()[-2000:]
        if connecting:
            message = "SSH 连接失败，请检查主机、端口和认证信息。"
            details = details or str(cause or "")
        else:
            message = "SSH 连接已中断或认证失败；操作结果可能未确认，请检查后重新连接。"
        return ConnectionError(message + ("\n" + details if details else ""))

    def call(self, op: str, *, timeout: float = 20, **payload):
        with self._lock:
            if self._closed.is_set() or self.process.poll() is not None:
                raise self._failure()
            self._counter += 1
            message = json.dumps({"id": self._counter, "op": op, **payload}, ensure_ascii=True).encode() + b"\n"
            if len(message) > remote_helper.MAX_FRAME:
                raise ValueError("SSH request exceeds frame limit")
            try:
                self.process.stdin.write(message)
                self.process.stdin.flush()
                response = self._responses.get(timeout=timeout)
                if response is None or response.get("id") != self._counter:
                    raise self._failure()
            except (OSError, ValueError, queue.Empty, ConnectionError):
                self.close()
                raise self._failure() from None
            if "error" in response:
                raise ValueError(response["error"])
            return response["result"]

    def _heartbeat(self):
        while not self._closed.wait(10):
            try:
                self.call("ping")
            except (ValueError, ConnectionError):
                break

    def close(self):
        self._closed.set()
        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
        self._error_reader.join(timeout=1)
        for stream in (self.process.stdout, self.process.stderr):
            # Reader threads own blocking reads; process exit delivers EOF.
            if stream and self.process.poll() is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
