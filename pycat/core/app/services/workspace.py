"""Application-owned workspace connections shared by tools, prompts and content."""
from __future__ import annotations

import hashlib
from pathlib import Path
import threading

from pycat.core.hosts.files import SshFiles
from pycat.core.hosts.ssh import SshConnection
from pycat.models.workspace import WorkspaceLocation
from pycat.models.session_paths import resolve_session_root


class WorkspaceService:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self._connections = {}
        self._lock = threading.RLock()
        self._host_locks = {}
        self._pending = set()
        self._closed = False

    def connect(self, host, *, port=None, password: str | None = None, prompt=None, cancel_event=None):
        location = WorkspaceLocation.ssh(host, "/", port=port)
        cancel_event = cancel_event or threading.Event()
        with self._lock:
            if self._closed:
                raise RuntimeError("Workspace service is closed")
            host_lock = self._host_locks.setdefault(location.endpoint, threading.Lock())
            self._pending.add(cancel_event)
        try:
            with host_lock:
                return self._connect_host(location, password=password, prompt=prompt, cancel_event=cancel_event)
        finally:
            with self._lock:
                self._pending.discard(cancel_event)

    def _connect_host(self, location, *, password, prompt, cancel_event):
        with self._lock:
            old = self._connections.get(location.endpoint)
        if cancel_event.is_set():
            raise ConnectionError("SSH 连接已取消")
        if old is not None:
            # Explicit reconnect is allowed only after connection loss; do not kill active jobs.
            if old.process.poll() is None and not old.closed:
                return old.info
            old.close()
        connection = SshConnection.connect(location.host, port=location.port, password=password,
            prompt=prompt, cancel_event=cancel_event)
        with self._lock:
            if self._closed or cancel_event.is_set():
                connection.close()
                raise ConnectionError("SSH 连接已取消")
            self._connections[location.endpoint] = connection
        return connection.info

    def files(self, work_dir):
        location = WorkspaceLocation.parse(work_dir)
        if not location.is_remote:
            return None
        with self._lock:
            missing = location.endpoint not in self._connections
        if missing:
            self.connect(location.host, port=location.port)
        with self._lock:
            return SshFiles(location, self._connections[location.endpoint])

    def directories(self, host, path, *, port=None):
        location = WorkspaceLocation.ssh(host, path, port=port)
        result = self.files(location.value).call("list", limit=1000)
        return [row["path"] for row in result["entries"] if row["type"] == "directory"]

    def validate(self, work_dir):
        location = WorkspaceLocation.parse(work_dir)
        if location.is_remote:
            metadata = self.files(location.value).stat(location.root)
            if not metadata["dir"]:
                raise ValueError("远端工作区不存在或不是目录")
            return WorkspaceLocation.ssh(location.host, metadata["path"], port=location.port).value
        local = str(work_dir or "").strip()
        if local and not Path(local).expanduser().is_dir():
            raise ValueError(f"Workspace not found: {local}")
        return str(Path(local).expanduser().resolve()) if local else ""

    def materialize(self, conversation, path, *, expected="", files=None, max_bytes=128 * 1024 * 1024):
        files = files or self.files(conversation.work_dir)
        metadata = files.stat(path, digest=True)
        if not metadata["file"] or metadata["size"] > max_bytes:
            raise ValueError("Remote file exceeds the supported preview/read limit")
        if expected and metadata["digest"] != expected:
            raise ValueError("内容已变化，引用的历史版本不可用；请重新核实来源。")
        # Disposable, digest-keyed views under the existing session owner; no second content store.
        name = "content" + files.absolute(path).suffix[:20]
        target = resolve_session_root(conversation.work_dir, conversation.id, data_dir=self.data_dir) / "remote-view" / metadata["digest"][:16] / name
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == metadata["digest"]:
            return target
        data = files.read_bytes(path, max_bytes=max_bytes)
        if hashlib.sha256(data).hexdigest() != metadata["digest"]:
            raise ValueError("Remote file changed during download")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def close(self):
        with self._lock:
            self._closed = True
            for event in self._pending:
                event.set()
            connections, self._connections = list(self._connections.values()), {}
        for connection in connections:
            connection.close()
