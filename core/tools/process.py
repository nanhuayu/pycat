"""Process execution helpers used by command-oriented tools."""
from __future__ import annotations

import locale
import logging
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from core.config import ShellConfig, load_app_config


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


def load_shell_config() -> ShellConfig:
    try:
        return load_app_config().shell
    except Exception as exc:
        logger.debug("Failed to load shell config from app settings: %s", exc)
        return ShellConfig()


def _windows_path_to_wsl(path: Path) -> str:
    text = str(path.resolve())
    if len(text) >= 2 and text[1] == ":":
        drive = text[0].lower()
        tail = text[2:].replace("\\", "/")
        return f"/mnt/{drive}{tail}"
    return text.replace("\\", "/")


def build_shell_command(command: str, cwd: Path, *, shell_config: ShellConfig | None = None) -> list[str]:
    config = shell_config or load_shell_config()
    backend = str(getattr(config, "backend", "cmd") or "cmd").strip().lower() or "cmd"

    if backend == "powershell":
        executable = str(getattr(config, "powershell_executable", "") or "powershell.exe").strip() or "powershell.exe"
        return [executable, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]

    if backend == "wsl":
        executable = str(getattr(config, "wsl_executable", "") or "wsl.exe").strip() or "wsl.exe"
        distro = str(getattr(config, "wsl_distro", "") or "").strip()
        linux_cwd = _windows_path_to_wsl(cwd)
        shell_command = f"cd {shlex.quote(linux_cwd)} && {command}" if linux_cwd else command
        argv = [executable]
        if distro:
            argv.extend(["-d", distro])
        argv.extend(["--", "sh", "-lc", shell_command])
        return argv

    executable = str(getattr(config, "cmd_executable", "") or "").strip() or os.environ.get("ComSpec") or "cmd.exe"
    return [executable, "/d", "/s", "/c", command]


@dataclass(frozen=True)
class CommandExecutionRequest:
    command: str
    cwd: Path
    timeout_sec: int = 600
    background: bool = False


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

    def to_display_text(self, cwd: Path) -> str:
        if self.process_id is not None:
            return (
                f"Command started in background in '{cwd}' via {self.backend or 'default shell'}. "
                f"process_id={self.process_id}, pid={self.pid}. "
                "Use shell_status, shell_logs, shell_wait, or shell_kill to manage it."
            )

        if self.timed_out:
            return (
                f"Command timed out after execution in '{cwd}' via {self.backend or 'default shell'}. "
                "Use 'background: true' for long-running commands."
            )

        parts = [f"Command executed in '{cwd}' via {self.backend or 'default shell'}. Exit code: {self.exit_code}"]
        if self.stdout:
            parts.append(f"Stdout:\n{self.stdout}")
        if self.stderr:
            parts.append(f"Stderr:\n{self.stderr}")
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

    def to_display_text(self) -> str:
        status = "running" if self.running else f"exited({self.exit_code})"
        return (
            f"process_id={self.process_id}\n"
            f"pid={self.pid}\n"
            f"status={status}\n"
            f"backend={self.backend}\n"
            f"cwd={self.cwd}\n"
            f"command={self.command}\n"
            f"log={self.log_path}"
        )


@dataclass
class _BackgroundProcessRecord:
    process_id: str
    command: str
    cwd: Path
    backend: str
    log_path: Path
    started_at: float
    process: subprocess.Popen
    log_handle: BinaryIO | None = None
    exit_code: int | None = None
    ended_at: float | None = None


class BackgroundProcessManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, _BackgroundProcessRecord] = {}

    def start(self, request: CommandExecutionRequest, *, shell_config: ShellConfig) -> BackgroundProcessSnapshot:
        log_dir = request.cwd / ".pycat" / "process_logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        process_id = uuid.uuid4().hex[:12]
        log_path = log_dir / f"{process_id}.log"
        log_handle = log_path.open("ab")
        argv = build_shell_command(request.command, request.cwd, shell_config=shell_config)

        proc = subprocess.Popen(
            argv,
            shell=False,
            cwd=str(request.cwd),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=_build_subprocess_env(inherit_env=shell_config.inherit_env),
        )

        record = _BackgroundProcessRecord(
            process_id=process_id,
            command=request.command,
            cwd=request.cwd,
            backend=shell_config.backend,
            log_path=log_path,
            started_at=time.time(),
            process=proc,
            log_handle=log_handle,
        )
        with self._lock:
            self._records[process_id] = record
        return self._snapshot(record)

    def status(self, process_id: str) -> BackgroundProcessSnapshot:
        record = self._get_record(process_id)
        self._refresh(record)
        return self._snapshot(record)

    def read_logs(self, process_id: str, tail_bytes: int = _DEFAULT_LOG_TAIL_BYTES, *, shell_config: ShellConfig | None = None) -> str:
        record = self._get_record(process_id)
        self._refresh(record)
        if not record.log_path.exists():
            return ""

        raw = record.log_path.read_bytes()
        if tail_bytes > 0 and len(raw) > tail_bytes:
            raw = raw[-tail_bytes:]
        preferred_encoding = None if shell_config is None else shell_config.output_encoding
        return truncate_process_output(decode_subprocess_output(raw, preferred_encoding=preferred_encoding).strip())

    def wait(self, process_id: str, timeout_sec: int | None = None) -> BackgroundProcessSnapshot:
        record = self._get_record(process_id)
        try:
            record.process.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"Process {process_id} did not finish within {timeout_sec}s") from exc
        self._refresh(record)
        return self._snapshot(record)

    def kill(self, process_id: str) -> BackgroundProcessSnapshot:
        record = self._get_record(process_id)
        self._refresh(record)
        if record.exit_code is None:
            record.process.terminate()
            try:
                record.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                record.process.kill()
                record.process.wait(timeout=5)
        self._refresh(record)
        return self._snapshot(record)

    def _get_record(self, process_id: str) -> _BackgroundProcessRecord:
        with self._lock:
            record = self._records.get((process_id or "").strip())
        if not record:
            raise KeyError(f"Unknown process_id: {process_id}")
        return record

    def _refresh(self, record: _BackgroundProcessRecord) -> None:
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
        )


_BACKGROUND_MANAGER = BackgroundProcessManager()


class CommandExecutor:
    """Thin wrapper around subprocess for consistent command execution behavior."""

    def execute(self, request: CommandExecutionRequest) -> CommandExecutionResult:
        shell_config = load_shell_config()
        if request.background:
            snapshot = _BACKGROUND_MANAGER.start(request, shell_config=shell_config)
            return CommandExecutionResult(
                exit_code=None,
                pid=snapshot.pid,
                process_id=snapshot.process_id,
                running=True,
                backend=snapshot.backend,
            )

        try:
            argv = build_shell_command(request.command, request.cwd, shell_config=shell_config)
            proc = subprocess.run(
                argv,
                shell=False,
                cwd=str(request.cwd),
                capture_output=True,
                text=False,
                timeout=request.timeout_sec,
                env=_build_subprocess_env(inherit_env=shell_config.inherit_env),
            )
            return CommandExecutionResult(
                exit_code=proc.returncode,
                stdout=truncate_process_output(
                    decode_subprocess_output(proc.stdout, preferred_encoding=shell_config.output_encoding).strip()
                ),
                stderr=truncate_process_output(
                    decode_subprocess_output(proc.stderr, preferred_encoding=shell_config.output_encoding).strip()
                ),
                backend=shell_config.backend,
            )
        except subprocess.TimeoutExpired:
            return CommandExecutionResult(exit_code=None, timed_out=True, backend=shell_config.backend)

    def status(self, process_id: str) -> BackgroundProcessSnapshot:
        return _BACKGROUND_MANAGER.status(process_id)

    def read_logs(self, process_id: str, tail_bytes: int = _DEFAULT_LOG_TAIL_BYTES) -> str:
        return _BACKGROUND_MANAGER.read_logs(process_id, tail_bytes=tail_bytes, shell_config=load_shell_config())

    def wait(self, process_id: str, timeout_sec: int | None = None) -> BackgroundProcessSnapshot:
        return _BACKGROUND_MANAGER.wait(process_id, timeout_sec=timeout_sec)

    def kill(self, process_id: str) -> BackgroundProcessSnapshot:
        return _BACKGROUND_MANAGER.kill(process_id)
