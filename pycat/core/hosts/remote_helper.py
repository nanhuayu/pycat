"""Connection-bound helper: standard library only, transmitted and run in memory.

No PyCat imports, installation, listener, credentials or durable agent state.
stdout is exclusively JSONL. EOF/lease expiry terminates owned process groups.
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

PROTOCOL = 2
CHUNK = 65536
MAX_FILE = 128 * 1024 * 1024
MAX_FRAME = 2 * 1024 * 1024
LEASE_SECONDS = 45


if os.name == "nt":
    import ctypes
    from ctypes import wintypes as w

    class JobLimits(ctypes.Structure):
        _fields_ = [("times", ctypes.c_int64 * 2), ("flags", w.DWORD),
                    ("working_set", ctypes.c_size_t * 2), ("processes", w.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD),
                    ("io", ctypes.c_uint64 * 6), ("memory", ctypes.c_size_t * 4)]

    class ThreadEntry(ctypes.Structure):
        _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("tid", w.DWORD), ("pid", w.DWORD),
                    ("priority", w.LONG), ("delta", w.LONG), ("flags", w.DWORD)]

    class WindowsJob:
        """Own descendants even if the leader exits; never let a child escape before assignment."""
        def __init__(self, proc):
            self.api = ctypes.WinDLL("kernel32", use_last_error=True)
            signatures = {
                "CreateJobObjectW": ([w.LPVOID, w.LPCWSTR], w.HANDLE),
                "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD], w.BOOL),
                "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
                "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
                "CreateToolhelp32Snapshot": ([w.DWORD, w.DWORD], w.HANDLE),
                "Thread32First": ([w.HANDLE, ctypes.POINTER(ThreadEntry)], w.BOOL),
                "Thread32Next": ([w.HANDLE, ctypes.POINTER(ThreadEntry)], w.BOOL),
                "OpenThread": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
                "ResumeThread": ([w.HANDLE], w.DWORD),
                "CloseHandle": ([w.HANDLE], w.BOOL),
            }
            for name, (args, result) in signatures.items():
                function = getattr(self.api, name)
                function.argtypes, function.restype = args, result
            self.handle = self.api.CreateJobObjectW(None, None)
            try:
                limits = JobLimits()
                limits.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                self.check(self.handle)
                self.check(self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
                handle = self.api.OpenProcess(0x0100 | 0x0001, False, proc.pid)  # SET_QUOTA | TERMINATE
                self.check(handle)
                try:
                    self.check(self.api.AssignProcessToJobObject(self.handle, handle))
                finally:
                    self.api.CloseHandle(handle)
                # Popen closes the initial thread handle. Find and resume that
                # suspended thread using documented Toolhelp APIs, after assignment.
                snapshot = self.api.CreateToolhelp32Snapshot(4, 0)
                if snapshot == ctypes.c_void_p(-1).value:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    entry = ThreadEntry()
                    entry.size = ctypes.sizeof(entry)
                    found = self.api.Thread32First(snapshot, ctypes.byref(entry))
                    while found:
                        if entry.pid == proc.pid:
                            thread = self.api.OpenThread(0x0002, False, entry.tid)
                            self.check(thread)
                            try:
                                if self.api.ResumeThread(thread) == 0xFFFFFFFF:
                                    raise ctypes.WinError(ctypes.get_last_error())
                            finally:
                                self.api.CloseHandle(thread)
                            return
                        found = self.api.Thread32Next(snapshot, ctypes.byref(entry))
                    raise OSError("Cannot resume the owned remote process")
                finally:
                    self.api.CloseHandle(snapshot)
            except BaseException:
                self.close()
                proc.kill()
                proc.wait(timeout=3)
                proc.stdout.close()
                raise

        @staticmethod
        def check(value):
            if not value:
                raise ctypes.WinError(ctypes.get_last_error())

        def close(self):
            if self.handle:
                self.api.CloseHandle(self.handle)
                self.handle = None


def digest(path):
    if not path.is_file():
        return ""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class Helper:
    def __init__(self):
        self.transfers = {}
        self.processes = {}
        self.lock = threading.Lock()

    def path(self, request):
        root = Path(request["root"]).expanduser().resolve(strict=True)
        raw = Path(request.get("path") or ".").expanduser()
        path = (raw if raw.is_absolute() else root / raw).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Path is outside authorized root")
        return path

    def metadata(self, path, with_digest=False):
        if not path.exists():
            return {"path": str(path), "exists": False, "file": False, "dir": False, "size": 0, "digest": ""}
        st = path.stat()
        result = {"path": str(path), "exists": True, "file": path.is_file(), "dir": path.is_dir(),
                  "size": st.st_size, "version": [st.st_size, st.st_mtime_ns, st.st_ino]}
        if with_digest:
            if st.st_size > MAX_FILE:
                raise ValueError("File exceeds 128 MiB transfer limit")
            result["digest"] = digest(path)
            if self.metadata(path)["version"] != result["version"]:
                raise ValueError("File changed while hashing")
        return result

    def handle(self, op, r):
        if op == "hello":
            return {"protocol": PROTOCOL, "python": list(sys.version_info[:3]), "platform": sys.platform,
                    "home": Path.home().as_posix(), "rg": bool(shutil.which("rg")),
                    "executable": sys.executable, "temp": Path(tempfile.gettempdir()).as_posix(),
                    "roots": [f"{c}:/" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if Path(f"{c}:/").is_dir()] if os.name == "nt" else ["/"],
                    "digest": globals().get("SOURCE_DIGEST", "")}
        if op == "ping":
            return True
        if op.startswith("process_"):
            return self.process(op, r)
        if op == "write_abort":
            transfer = self.transfers.pop(r["token"], None)
            if transfer:
                transfer["stream"].close()
                transfer["staging"].unlink(missing_ok=True)
            return True
        if op == "write_chunk":
            transfer = self.transfers[r["token"]]
            data = base64.b64decode(r["data"], validate=True)
            if len(data) > CHUNK or transfer["stream"].tell() + len(data) > MAX_FILE:
                raise ValueError("Transfer limit exceeded")
            transfer["stream"].write(data)
            return True
        if op == "write_finish":
            transfer = self.transfers.pop(r["token"])
            stream, staging = transfer["stream"], transfer["staging"]
            try:
                stream.close()
                path = self.path(transfer["request"])
                if digest(staging) != r["digest"]:
                    raise ValueError("Transfer digest mismatch")
                expected = transfer["request"].get("expected")
                if expected is not None and digest(path) != expected:
                    raise ValueError("File changed before write; re-read and retry")
                if path.exists():
                    os.chmod(staging, path.stat().st_mode & 0o777)
                os.replace(staging, path)
                return self.metadata(path, True)
            finally:
                staging.unlink(missing_ok=True)
        path = self.path(r)
        if op == "stat":
            return self.metadata(path, r.get("digest", False))
        if op == "read":
            metadata = self.metadata(path)
            if r.get("version") != metadata.get("version"):
                raise ValueError("File changed during read")
            if not metadata["file"] or metadata["size"] > MAX_FILE:
                raise ValueError("Not a regular file or file exceeds 128 MiB")
            with path.open("rb") as stream:
                stream.seek(max(0, int(r.get("offset", 0))))
                data = stream.read(min(CHUNK, max(1, int(r.get("size", CHUNK)))))
            if self.metadata(path)["version"] != metadata["version"]:
                raise ValueError("File changed during read")
            return base64.b64encode(data).decode("ascii")
        if op == "write_begin":
            if len(self.transfers) >= 32:
                raise ValueError("Too many unfinished transfers; reconnect")
            path.parent.mkdir(parents=True, exist_ok=True)
            stream = tempfile.NamedTemporaryFile(prefix=".pycat-upload-", dir=path.parent, delete=False)
            token = uuid.uuid4().hex
            self.transfers[token] = {"stream": stream, "staging": Path(stream.name), "request": r}
            return {"token": token}
        if op == "delete":
            if path == Path(r["root"]).expanduser().resolve():
                raise ValueError("Cannot delete the authorized root")
            if path.is_dir():
                shutil.rmtree(path) if r.get("recursive") else path.rmdir()
            else:
                path.unlink()
            return True
        if op == "list":
            limit = min(2000, max(1, int(r.get("limit", 200))))
            entries = []
            for child in self.walk(path, recursive=r.get("recursive", False)):
                if len(entries) > limit:
                    break
                try:
                    child = self.path({**r, "path": str(child)})
                    metadata = self.metadata(child)
                    entries.append({"path": str(child), "type": "directory" if metadata["dir"] else "file", "size": metadata["size"]})
                except (OSError, ValueError):
                    continue
            return {"entries": entries[:limit], "truncated": len(entries) > limit}
        if op == "search":
            return self.search(path, r)
        raise ValueError("Unknown workspace operation")

    def walk(self, root, recursive=True, visible=False):
        deadline = time.monotonic() + 10
        count = 0
        for folder, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not (Path(folder) / d).is_symlink()
                and not (visible and (d.startswith(".") or d in {"node_modules", "__pycache__", "venv", "dist", "build"})))
            for name in sorted([*dirs, *files]):
                count += 1
                if count > 50000 or time.monotonic() > deadline:
                    raise TimeoutError("Directory scan exceeded limit; narrow the path")
                yield Path(folder) / name
            if not recursive:
                break

    def search(self, path, r):
        query = str(r.get("query", ""))
        limit = min(500, max(1, int(r.get("limit", 50))))
        if executable := shutil.which("rg"):
            return self.search_rg(path, r, executable, limit)
        if r.get("regex"):
            raise ValueError("Regex search requires ripgrep on the remote host; use literal text or install rg there")
        found = []
        pattern = r.get("glob", "")
        # Bounded literal fallback. Explicitly reports that ignore-file rules are not interpreted.
        for candidate in self.walk(path, visible=True):
            relative = candidate.relative_to(path).as_posix()
            if any(part.startswith(".") for part in candidate.relative_to(path).parts):
                continue
            if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size > 2 * 1024 * 1024:
                continue
            if pattern and not fnmatch.fnmatchcase(relative, pattern):
                continue
            candidate = self.path({**r, "path": str(candidate)})
            with candidate.open("rb") as stream:
                raw = stream.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024 or b"\0" in raw:
                continue
            for number, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
                if query in line:
                    found.append({"path": str(candidate), "line": number, "text": line[:300]})
                    if len(found) > limit:
                        return {"matches": found[:limit], "truncated": True, "backend": "python", "ignore_files": False}
        return {"matches": found, "truncated": False, "backend": "python", "ignore_files": False}

    def search_rg(self, path, r, executable, limit):
        args = [executable, "--json", "--no-config", "--color=never", "--sort=path", "--max-filesize=2M"]
        if not r.get("regex"):
            args.append("--fixed-strings")
        if r.get("glob"):
            args.extend(["--glob", r["glob"]])
        args.extend(["--", r["query"], str(path)])
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        timer = threading.Timer(10, proc.kill)
        timer.daemon = True
        timer.start()
        found = []
        try:
            while line := proc.stdout.readline(MAX_FRAME + 1):
                if len(line) > MAX_FRAME:
                    raise ValueError("Search line exceeds output limit; narrow the query")
                try:
                    event = json.loads(line)
                except ValueError:
                    raise ValueError("rg search failed: " + line.decode(errors="replace")[:1000]) from None
                if event.get("type") != "match":
                    continue
                data = event["data"]
                name, text = data["path"].get("text"), data["lines"].get("text")
                if name is None or text is None:
                    continue
                candidate = self.path({**r, "path": name})
                found.append({"path": str(candidate), "line": data["line_number"], "text": text.rstrip("\r\n")[:300]})
                if len(found) > limit:
                    break
            if len(found) <= limit and proc.wait(timeout=2) not in (0, 1):
                raise ValueError("rg search timed out or failed; narrow the path")
            return {"matches": found[:limit], "truncated": len(found) > limit, "backend": "rg", "ignore_files": True}
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=3)
            proc.stdout.close()

    def process(self, op, r):
        if op == "process_start":
            if len(self.processes) >= 128:
                raise ValueError("Connection process limit reached; reconnect after jobs finish")
            cwd = self.path(r)
            program = r.get("program")
            if program:
                invocation = [program, *r.get("argv", [])]
            elif os.name == "nt":
                shell = subprocess.list2cmdline([os.environ.get("COMSPEC", "cmd.exe")])
                invocation = f'{shell} /d /s /c "{r["command"]}"'
            else:
                invocation = ["/bin/sh", "-c", r["command"]]
            with tempfile.TemporaryFile() as stream:
                stream.write(str(r.get("stdin") or "").encode("utf-8"))
                stream.seek(0)
                proc = subprocess.Popen(invocation, cwd=cwd, stdin=stream, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, start_new_session=os.name != "nt",
                    creationflags=(subprocess.CREATE_NO_WINDOW | 0x00000004) if os.name == "nt" else 0,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
            job = WindowsJob(proc) if os.name == "nt" else None
            key = uuid.uuid4().hex
            record = {"proc": proc, "job": job, "buffer": bytearray(), "base": 0, "done": threading.Event()}
            self.processes[key] = record
            threading.Thread(target=self.drain, args=(record,), daemon=True).start()
            return {"process_id": key, "pid": proc.pid}
        record = self.processes[r["process_id"]]
        proc = record["proc"]
        if op == "process_release":
            if proc.poll() is None or not record["done"].is_set():
                raise ValueError("Cannot release a running process")
            self.kill(record)
            del self.processes[r["process_id"]]
            return True
        if op == "process_kill":
            self.kill(record)
        elif op not in ("process_status", "process_read"):
            raise ValueError("Unknown process operation")
        result = {"exit_code": proc.poll(), "drained": record["done"].is_set()}
        if op == "process_read":
            with self.lock:
                cursor = max(record["base"], int(r.get("cursor", 0)))
                offset = cursor - record["base"]
                data = bytes(record["buffer"][offset:offset + CHUNK])
                result.update(data=base64.b64encode(data).decode(), cursor=cursor, next_cursor=cursor + len(data))
        return result

    def drain(self, record):
        try:
            while data := record["proc"].stdout.read1(CHUNK):
                with self.lock:
                    record["buffer"].extend(data)
                    extra = max(0, len(record["buffer"]) - 4 * 1024 * 1024)
                    if extra:
                        del record["buffer"][:extra]
                        record["base"] += extra
        finally:
            record["proc"].stdout.close()
            record["done"].set()

    @staticmethod
    def kill(record):
        proc = record["proc"]
        if record["job"] is not None:
            record["job"].close()
            proc.wait(timeout=3)
            return
        # Also clean surviving children after the group leader has exited.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=3)

    def close(self):
        for transfer in self.transfers.values():
            transfer["stream"].close()
            transfer["staging"].unlink(missing_ok=True)
        for record in self.processes.values():
            try:
                self.kill(record)
            except (OSError, subprocess.TimeoutExpired):
                pass


def main():
    helper = Helper()
    incoming = queue.Queue(maxsize=8)
    def read():
        try:
            pending = bytearray()
            while True:
                chunk = os.read(sys.stdin.fileno(), CHUNK)
                if not chunk:
                    break
                pending.extend(chunk)
                while b"\n" in pending:
                    end = pending.index(b"\n") + 1
                    if end > MAX_FRAME:
                        return
                    incoming.put(bytes(pending[:end]))
                    del pending[:end]
                if len(pending) > MAX_FRAME:
                    break
        finally:
            incoming.put(None)
    threading.Thread(target=read, daemon=True).start()
    try:
        while True:
            try:
                line = incoming.get(timeout=LEASE_SECONDS)
            except queue.Empty:
                break
            if line is None:
                break
            request = json.loads(line)
            try:
                result = {"id": request["id"], "result": helper.handle(request["op"], request)}
            except Exception as exc:
                result = {"id": request["id"], "error": f"{type(exc).__name__}: {exc}"}
            sys.stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
            sys.stdout.flush()
    finally:
        helper.close()


if __name__ == "__main__":
    main()
