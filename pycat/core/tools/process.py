"""Process execution helpers used by command-oriented tools."""
from __future__ import annotations

import codecs
import locale
import logging
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import BinaryIO

from pycat.models.contracts.config import ShellConfig
from pycat.core.hosts.process import SshProcess
from pycat.core.tools.shell import process_invocation, resolve_shell, shell_command
from pycat.core.tools.terminal import TerminalProcess


logger = logging.getLogger(__name__)


_DANGEROUS_PATTERNS = [
    r"\brm\s+-rf\b",
    r"\brm\s+-r\b",
    r"\bdel\s+/[sS]",
    r"\bformat\b",
    r"\bmkfs\b",
    r"\bdd\s+",
    r"\b>\s*/dev/",
    r"\bgit\s+push\s+.*--force",
    r"\bgit\s+reset\s+--hard",
    r"\bdrop\s+database\b",
    r"\bdrop\s+table\b",
    r"\btruncate\s+",
    r"\bshutdown\b",
    r"\breboot\b",
]
_MAX_OUTPUT_LINES = 2000
_MAX_OUTPUT_BYTES = 50 * 1024
_DEFAULT_LOG_TAIL_BYTES = 12 * 1024


def _dedupe_encodings(encodings: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for encoding in encodings:
        name = str(encoding or "").strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result


def _looks_like_utf16(raw: bytes) -> bool:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return True
    sample = raw[:128]
    if not sample:
        return False
    nul_count = sample.count(0)
    return nul_count >= max(2, len(sample) // 6)


def _build_subprocess_env(*, inherit_env: bool = True) -> dict[str, str]:
    env = dict(os.environ) if inherit_env else {}
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def decode_subprocess_output(data: object, preferred_encoding: str | None = None) -> str:
    if not data:
        return ""
    if isinstance(data, str):
        return data
    if not isinstance(data, (bytes, bytearray)):
        try:
            return str(data)
        except Exception:
            return ""

    raw = bytes(data)
    encodings: list[str] = []
    if _looks_like_utf16(raw):
        encodings.extend(["utf-16", "utf-16-le", "utf-16-be"])

    preferred = str(preferred_encoding or "").strip().lower()
    if preferred and preferred not in {"auto", "system"}:
        encodings.append(preferred)

    encodings.extend(["utf-8", "utf-8-sig"])

    preferred_system_encoding = str(locale.getpreferredencoding(False) or "").strip()
    if preferred_system_encoding:
        encodings.append(preferred_system_encoding)

    encodings.extend(["gb18030", "gbk", "mbcs"])

    for enc in _dedupe_encodings(encodings):
        try:
            text = raw.decode(enc)
            if enc.startswith("utf-8") and "\x00" in text and _looks_like_utf16(raw):
                continue
            return text
        except UnicodeDecodeError:
            continue
        except Exception as exc:
            logger.debug("Unexpected subprocess decode failure for encoding %s: %s", enc, exc)
    return raw.decode("utf-8", errors="replace")


def truncate_process_output(text: str) -> str:
    if len(text) > _MAX_OUTPUT_BYTES:
        text = text[:_MAX_OUTPUT_BYTES] + f"\n\n... [truncated at {_MAX_OUTPUT_BYTES} bytes]"
    lines = text.splitlines()
    if len(lines) > _MAX_OUTPUT_LINES:
        text = "\n".join(lines[:_MAX_OUTPUT_LINES]) + f"\n\n... [{len(lines) - _MAX_OUTPUT_LINES} lines truncated]"
    return text


def is_dangerous_command(command: str) -> bool:
    cmd_lower = command.lower()
    return any(re.search(pattern, cmd_lower) for pattern in _DANGEROUS_PATTERNS)


def build_shell_command(command: str, cwd: Path, *, shell_config: ShellConfig | None = None) -> list[str]:
    return shell_command(command, cwd, shell_config or ShellConfig())


@dataclass(frozen=True)
class CommandExecutionRequest:
    command: str
    cwd: Path
    timeout_sec: int = 600
    background: bool = False
    conversation_id: str = ""
    session_root: Path | None = None
    program: str = ""
    argv: tuple[str, ...] = ()
    stdin: str | None = None
    remote: object | None = None
    interactive: bool = False
    columns: int = 100
    rows: int = 30
    controller: str = "agent"


@dataclass(frozen=True)
class CommandExecutionResult:
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    pid: int | None = None
    process_id: str | None = None
    timed_out: bool = False
    running: bool = False
    backend: str = ""
    error: str = ""

    def to_display_text(self, cwd: Path) -> str:
        if self.error:
            return self.error + (f"\nOutput before disconnect:\n{self.stdout}" if self.stdout else "")
        if self.timed_out and self.process_id is not None:
            parts = [
                f"Command still running after the foreground wait in '{cwd}' via {self.backend or 'default shell'}; "
                "it was NOT killed and continues in background.",
                f"process_id={self.process_id}, pid={self.pid}.",
            ]
            if self.stdout:
                parts.append(f"output_tail:\n{self.stdout}")
            parts.append(
                "Note: use shell__read(process_id=\"...\", wait_seconds=N) to poll for completion, "
                "shell__kill(process_id=\"...\") to terminate it, or continue with other work and check back later."
            )
            return "\n".join(parts)

        if self.running and self.process_id is not None:
            return (
                f"Command started in background in '{cwd}' via {self.backend or 'default shell'}. "
                f"process_id={self.process_id}, pid={self.pid}. "
                "Use shell__read to inspect it or shell__kill to terminate it."
            )

        parts = [f"Command executed in '{cwd}' via {self.backend or 'default shell'}. Exit code: {self.exit_code}",
                 f"process_id={self.process_id}, pid={self.pid}"]
        if self.stdout:
            parts.append(f"Output:\n{self.stdout}")
        return "\n\n".join(parts)


@dataclass(frozen=True)
class BackgroundProcessSnapshot:
    process_id: str
    pid: int
    command: str
    cwd: Path
    backend: str
    log_path: Path
    running: bool
    exit_code: int | None
    started_at: float
    ended_at: float | None = None
    conversation_id: str = ""
    error: str = ""
    interactive: bool = False
    controller: str = "agent"

    @property
    def elapsed_sec(self) -> float:
        end = self.ended_at if self.ended_at is not None else time.time()
        return max(0.0, end - self.started_at)

    @property
    def log_bytes(self) -> int:
        try:
            return int(self.log_path.stat().st_size)
        except OSError:
            return 0

    @property
    def last_output_at(self) -> float | None:
        """Log mtime — the last time the process wrote output."""
        try:
            return float(self.log_path.stat().st_mtime)
        except OSError:
            return None

    def to_display_text(self) -> str:
        status = "unknown" if self.error else ("running" if self.running else f"exited({self.exit_code})")
        return (
            f"process_id={self.process_id}\n"
            f"pid={self.pid}\n"
            f"status={status}\n"
            f"backend={self.backend}\n"
            f"interactive={str(self.interactive).lower()}\ncontroller={self.controller}\n"
            f"cwd={self.cwd}\n"
            f"elapsed={self.elapsed_sec:.0f}s\n"
            f"log_bytes={self.log_bytes}\n"
            f"command={self.command}\n"
            f"log={self.log_path}" + (f"\nerror={self.error}" if self.error else "")
        )


@dataclass(frozen=True)
class ProcessReadChunk:
    """Incremental log read result with cursor bookkeeping."""

    snapshot: BackgroundProcessSnapshot
    output: str
    cursor: int
    next_cursor: int
    has_more: bool

    def to_display_text(self) -> str:
        body = self.snapshot.to_display_text()
        body += f"\ncursor={self.cursor}\nnext_cursor={self.next_cursor}\nhas_more={str(self.has_more).lower()}"
        if self.output:
            body += f"\noutput:\n{self.output}"
        return body


@dataclass
class _BackgroundProcessRecord:
    process_id: str
    command: str
    cwd: Path
    backend: str
    log_path: Path
    started_at: float
    process: subprocess.Popen
    conversation_id: str = ""
    log_handle: BinaryIO | None = None
    exit_code: int | None = None
    ended_at: float | None = None
    interactive: bool = False
    controller: str = "agent"
    encoding: str = "auto"
    lock: threading.RLock = field(default_factory=threading.RLock)


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Terminate a process and its children (best effort per platform)."""
    if isinstance(proc, (SshProcess, TerminalProcess)):
        proc.terminate()
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return
        except Exception as exc:
            logger.debug("taskkill /T failed for pid %s: %s", proc.pid, exc)
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            return
        except Exception as exc:
            logger.debug("killpg failed for pid %s: %s", proc.pid, exc)
    try:
        proc.terminate()
    except Exception as exc:
        logger.debug("terminate failed for pid %s: %s", proc.pid, exc)


class BackgroundProcessManager:
    """Owns every background process; one instance lives on ``ToolManager``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, _BackgroundProcessRecord] = {}

    def start(self, request: CommandExecutionRequest, *, shell_config: ShellConfig) -> BackgroundProcessSnapshot:
        if request.controller not in {"agent", "user"}:
            raise ValueError("Unknown terminal controller")
        if request.session_root is not None:
            log_dir = Path(request.session_root) / "process"
        else:
            log_dir = request.cwd / ".pycat" / "process_logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        process_id = uuid.uuid4().hex[:12]
        log_path = log_dir / f"{process_id}.log"
        log_handle = log_path.open("ab")
        try:
            if request.remote and request.interactive:
                raise ValueError("This SSH host does not yet provide interactive terminals; use a local Shell or a noninteractive command")
            # A temporary input file avoids a pipe writer blocking a background launch.
            if request.remote is not None:
                proc = SshProcess(request.remote, request, log_handle)
                backend = "ssh / " + str(request.remote.connection.info.get("platform", "unknown"))
            else:
                backend = "program" if request.program else resolve_shell(shell_config, interactive=request.interactive)[0]
                invocation = [request.program, *request.argv] if request.program else (
                    shell_command(request.command, request.cwd, shell_config, interactive=True) if request.interactive
                    else process_invocation(request.command, request.cwd, shell_config))
                env = _build_subprocess_env(inherit_env=shell_config.inherit_env)
                if request.interactive:
                    env.setdefault("TERM", "xterm-256color")
                    env.setdefault("COLORTERM", "truecolor")
                    proc = TerminalProcess(invocation, cwd=request.cwd, env=env, log=log_handle,
                                           columns=request.columns, rows=request.rows)
                else:
                    proc = self._start_pipe(invocation, request, env, log_handle)
        except BaseException:
            log_handle.close()
            raise

        record = _BackgroundProcessRecord(
            process_id=process_id, command=request.command or backend, cwd=request.cwd,
            backend=backend, log_path=log_path, started_at=time.time(), process=proc,
            conversation_id=str(request.conversation_id or ""), log_handle=log_handle,
            interactive=request.interactive,
            controller=request.controller,
            encoding="utf-8" if request.interactive else shell_config.output_encoding,
        )
        with self._lock:
            self._records[process_id] = record
        return self._snapshot(record)

    @staticmethod
    def _start_pipe(invocation, request, env, log_handle):
        with tempfile.TemporaryFile() as input_file:
            if request.stdin is not None:
                input_file.write(request.stdin.encode("utf-8"))
                input_file.seek(0)
            return subprocess.Popen(
                invocation, shell=False, cwd=str(request.cwd),
                stdin=input_file if request.stdin is not None else subprocess.DEVNULL,
                stdout=log_handle, stderr=subprocess.STDOUT, env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )

    def status(self, process_id: str, *, conversation_id: str | None = None) -> BackgroundProcessSnapshot:
        record = self._get_record(process_id, conversation_id)
        self._refresh(record)
        return self._snapshot(record)

    def read(
        self,
        process_id: str,
        *,
        conversation_id: str | None = None,
        cursor: int = 0,
        max_bytes: int = _DEFAULT_LOG_TAIL_BYTES,
        shell_config: ShellConfig | None = None,
    ) -> ProcessReadChunk:
        """Incremental log read: seeks to ``cursor`` and reads at most ``max_bytes``.

        The log file is never loaded whole; ``next_cursor``/``has_more`` let the
        caller page through output across calls.
        """
        record = self._get_record(process_id, conversation_id)
        self._refresh(record)
        try:
            size = int(record.log_path.stat().st_size)
        except OSError:
            size = 0
        start = max(0, min(int(cursor or 0), size))
        limit = max(1, min(32768, int(max_bytes or _DEFAULT_LOG_TAIL_BYTES)))
        raw = b""
        if size > start:
            with record.log_path.open("rb") as handle:
                handle.seek(start)
                raw = handle.read(limit + 8)
        encoding = self._encoding(record, shell_config)
        length = min(limit, len(raw))
        output, consumed = "", 0
        while raw:
            decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
            output = decoder.decode(raw[:length], final=record.exit_code is not None and start + length == size)
            consumed = length - len(decoder.getstate()[0])
            if consumed or length == len(raw):
                break
            length += 1
        if start == 0:
            output = output.removeprefix("\ufeff")
        next_cursor = start + consumed
        return ProcessReadChunk(
            snapshot=self._snapshot(record),
            output=output,
            cursor=start,
            next_cursor=next_cursor,
            has_more=next_cursor < size and consumed > 0,
        )

    def _encoding(self, record, config=None):
        if record.interactive:
            return "utf-8"
        preferred = config.output_encoding if config else record.encoding
        if preferred not in {"auto", "system", "utf-16"}:
            return preferred
        with record.log_path.open("rb") as handle:
            sample = handle.read(4096)
        if preferred == "utf-16" or (preferred == "auto" and _looks_like_utf16(sample)):
            return "utf-16-be" if sample.startswith(b"\xfe\xff") else "utf-16-le"
        if preferred not in {"auto", "system"}:
            return preferred
        if preferred == "system":
            return locale.getpreferredencoding(False)
        for candidate in _dedupe_encodings(["utf-8", locale.getpreferredencoding(False), "gb18030"]):
            try:
                codecs.getincrementaldecoder(candidate)().decode(sample, final=False)
                return candidate
            except (UnicodeError, LookupError):
                continue
        return "utf-8"

    def write(self, process_id: str, text: str, *, conversation_id: str, controller: str):
        if not isinstance(text, str) or len(text.encode("utf-8")) > 65536:
            raise ValueError("Terminal input must be text of at most 64 KiB")
        record = self._get_record(process_id, conversation_id)
        with record.lock:
            self._refresh(record)
            if not record.interactive:
                raise ValueError("Process is not interactive; start a new terminal")
            if record.exit_code is not None:
                raise ValueError("Terminal has exited")
            if controller != record.controller:
                raise PermissionError(f"Terminal input is controlled by {record.controller}")
            return record.process.write(text)

    def set_controller(self, process_id: str, controller: str, *, conversation_id: str):
        if controller not in {"user", "agent"}:
            raise ValueError("Unknown terminal controller")
        record = self._get_record(process_id, conversation_id)
        with record.lock:
            if not record.interactive:
                raise ValueError("Process is not interactive")
            record.controller = controller
            return self._snapshot(record)

    def respond(self, process_id: str, text: str, *, conversation_id: str):
        """Only terminal status replies bypass input ownership; never command text."""
        if not re.fullmatch(r"\x1b\[(?:[0-9]{1,3};[0-9]{1,3}R|0n|\?6c)", text):
            raise ValueError("Unsupported terminal response")
        record = self._get_record(process_id, conversation_id)
        with record.lock:
            self._refresh(record)
            if record.interactive and record.exit_code is None:
                return record.process.write(text)

    def resize(self, process_id: str, columns: int, rows: int, *, conversation_id: str):
        record = self._get_record(process_id, conversation_id)
        with record.lock:
            if record.interactive and record.exit_code is None:
                record.process.resize(max(2, min(500, int(columns))), max(1, min(300, int(rows))))

    def read_logs(
        self,
        process_id: str,
        tail_bytes: int = _DEFAULT_LOG_TAIL_BYTES,
        *,
        conversation_id: str | None = None,
        shell_config: ShellConfig | None = None,
    ) -> str:
        """Bounded tail read (``tail_bytes=0`` still caps at the output limit)."""
        record = self._get_record(process_id, conversation_id)
        self._refresh(record)
        try:
            size = int(record.log_path.stat().st_size)
        except OSError:
            return ""
        if size <= 0:
            return ""
        limit = int(tail_bytes) if tail_bytes > 0 else _MAX_OUTPUT_BYTES
        limit = max(1, min(limit, _MAX_OUTPUT_BYTES))
        with record.log_path.open("rb") as handle:
            handle.seek(max(0, size - limit))
            raw = handle.read(limit)
        encoding = self._encoding(record, shell_config)
        if size > limit:
            if encoding.startswith("utf-8"):
                raw = raw.lstrip(bytes(range(128, 192)))
            elif encoding.startswith("utf-16"):
                if (size - limit) % 2:
                    raw = raw[1:]
                if len(raw) >= 2 and 0xDC00 <= int.from_bytes(raw[:2], "big" if encoding.endswith("be") else "little") <= 0xDFFF:
                    raw = raw[2:]
        decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        return truncate_process_output(decoder.decode(raw, final=record.exit_code is not None).removeprefix("\ufeff"))

    def wait(
        self,
        process_id: str,
        timeout_sec: int | None = None,
        *,
        conversation_id: str | None = None,
    ) -> BackgroundProcessSnapshot:
        record = self._get_record(process_id, conversation_id)
        try:
            record.process.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"Process {process_id} did not finish within {timeout_sec}s") from exc
        self._refresh(record)
        return self._snapshot(record)

    def kill(self, process_id: str, *, conversation_id: str | None = None) -> BackgroundProcessSnapshot:
        record = self._get_record(process_id, conversation_id)
        self._refresh(record)
        if record.exit_code is None:
            _kill_process_tree(record.process)
            try:
                record.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                record.process.kill()
                record.process.wait(timeout=5)
        self._refresh(record)
        return self._snapshot(record)

    def list(
        self,
        *,
        include_exited: bool = False,
        conversation_id: str | None = None,
    ) -> list[BackgroundProcessSnapshot]:
        with self._lock:
            records = list(self._records.values())
        if conversation_id is not None:
            wanted = str(conversation_id or "")
            records = [record for record in records if record.conversation_id == wanted]
        snapshots = [self._snapshot(record) for record in records]
        return [snapshot for snapshot in snapshots if include_exited or snapshot.running]

    def kill_conversation(self, conversation_id: str) -> int:
        """Kill every still-running process bound to one conversation; returns count."""
        killed = 0
        for snapshot in self.list(include_exited=False, conversation_id=conversation_id):
            try:
                self.kill(snapshot.process_id, conversation_id=conversation_id)
                killed += 1
            except Exception as exc:
                logger.debug("Failed to kill conversation process %s: %s", snapshot.process_id, exc)
        return killed

    def kill_all(self) -> None:
        """Best-effort cleanup hook for application exit."""
        for snapshot in self.list(include_exited=False):
            try:
                self.kill(snapshot.process_id)
            except Exception as exc:
                logger.debug("Failed to kill background process %s: %s", snapshot.process_id, exc)

    def _get_record(self, process_id: str, conversation_id: str | None = None) -> _BackgroundProcessRecord:
        with self._lock:
            record = self._records.get((process_id or "").strip())
        if not record:
            raise KeyError(f"Unknown process_id: {process_id}")
        if conversation_id is not None and record.conversation_id != str(conversation_id or ""):
            # Session isolation: a foreign session's process is reported as unknown.
            raise KeyError(f"Unknown process_id: {process_id}")
        return record

    def _refresh(self, record: _BackgroundProcessRecord) -> None:
        with record.lock:
            self._refresh_locked(record)

    def _refresh_locked(self, record: _BackgroundProcessRecord) -> None:
        if record.exit_code is not None:
            return
        exit_code = record.process.poll()
        if exit_code is None:
            return
        record.exit_code = exit_code
        record.ended_at = time.time()
        if record.log_handle is not None:
            try:
                record.log_handle.flush()
                record.log_handle.close()
            except Exception as exc:
                logger.debug("Failed to close background process log handle %s: %s", record.process_id, exc)
            record.log_handle = None

    def _snapshot(self, record: _BackgroundProcessRecord) -> BackgroundProcessSnapshot:
        self._refresh(record)
        return BackgroundProcessSnapshot(
            process_id=record.process_id,
            pid=int(record.process.pid or 0),
            command=record.command,
            cwd=record.cwd,
            backend=record.backend,
            log_path=record.log_path,
            running=record.exit_code is None,
            exit_code=record.exit_code,
            started_at=record.started_at,
            ended_at=record.ended_at,
            conversation_id=record.conversation_id,
            error=getattr(record.process, "error", ""),
            interactive=record.interactive,
            controller=record.controller,
        )


class CommandExecutor:
    """Conversation-scoped adapter over an explicitly owned BackgroundProcessManager."""

    def __init__(
        self,
        manager: BackgroundProcessManager,
        shell_config: ShellConfig | None = None,
        *,
        conversation_id: str = "",
    ) -> None:
        if manager is None:
            raise ValueError("CommandExecutor requires an explicit BackgroundProcessManager")
        self._manager = manager
        self.shell_config = shell_config or ShellConfig()
        self.conversation_id = str(conversation_id or "")

    def execute(self, request: CommandExecutionRequest) -> CommandExecutionResult:
        shell_config = self.shell_config
        manager = self._manager
        if not request.conversation_id and self.conversation_id:
            request = replace(request, conversation_id=self.conversation_id)
        # Foreground execution is a bounded wait on a background start: when the
        # wait expires the process is NOT killed — it keeps running with a
        # process_id the agent can poll, terminate, or abandon.
        snapshot = manager.start(request, shell_config=shell_config)
        if request.background:
            return CommandExecutionResult(
                exit_code=None,
                pid=snapshot.pid,
                process_id=snapshot.process_id,
                running=True,
                backend=snapshot.backend,
            )

        try:
            done = manager.wait(snapshot.process_id, timeout_sec=request.timeout_sec)
        except TimeoutError:
            return CommandExecutionResult(
                exit_code=None,
                stdout=manager.read_logs(
                    snapshot.process_id,
                    tail_bytes=_DEFAULT_LOG_TAIL_BYTES,
                    shell_config=shell_config,
                ),
                pid=snapshot.pid,
                process_id=snapshot.process_id,
                timed_out=True,
                running=True,
                backend=snapshot.backend,
            )
        return CommandExecutionResult(
            exit_code=done.exit_code,
            process_id=snapshot.process_id,
            pid=snapshot.pid,
            error=done.error,
            stdout=manager.read_logs(snapshot.process_id, tail_bytes=0, shell_config=shell_config),
            backend=snapshot.backend,
        )

    def status(self, process_id: str) -> BackgroundProcessSnapshot:
        return self._manager.status(process_id, conversation_id=self.conversation_id)

    def read(self, process_id: str, *, cursor: int = 0, max_bytes: int = _DEFAULT_LOG_TAIL_BYTES) -> ProcessReadChunk:
        return self._manager.read(
            process_id,
            conversation_id=self.conversation_id,
            cursor=cursor,
            max_bytes=max_bytes,
            shell_config=self.shell_config,
        )

    def read_logs(self, process_id: str, tail_bytes: int = _DEFAULT_LOG_TAIL_BYTES) -> str:
        return self._manager.read_logs(
            process_id,
            tail_bytes=tail_bytes,
            conversation_id=self.conversation_id,
            shell_config=self.shell_config,
        )

    def wait(self, process_id: str, timeout_sec: int | None = None) -> BackgroundProcessSnapshot:
        return self._manager.wait(process_id, timeout_sec=timeout_sec, conversation_id=self.conversation_id)

    def kill(self, process_id: str) -> BackgroundProcessSnapshot:
        return self._manager.kill(process_id, conversation_id=self.conversation_id)

    def list(self, *, include_exited: bool = False) -> list[BackgroundProcessSnapshot]:
        return self._manager.list(include_exited=include_exited, conversation_id=self.conversation_id)
