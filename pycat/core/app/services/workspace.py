"""Application-owned workspace connections shared by tools, prompts and content."""
from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from pathlib import Path

from pycat.core.hosts.files import SshFiles
from pycat.core.hosts.ssh import SshConnection
from pycat.models.filenames import safe_filename
from pycat.models.session_paths import resolve_session_root
from pycat.models.workspace import WorkspaceLocation

TRANSFER_LIMIT = 128 * 1024 * 1024


def _local_path(work_dir, path="."):
    if not WorkspaceLocation.parse(work_dir).root:
        raise ValueError("请先选择工作区")
    root = Path(work_dir).expanduser().resolve()
    candidate = (root / path).resolve()
    if not root.is_dir() or not candidate.is_relative_to(root):
        raise ValueError("路径不在有效工作区内")
    return root, candidate


def _copy_file(source, destination, check_cancelled=None):
    """Explicit copies never replace a destination or remove the source."""
    if not source.is_file() or source.stat().st_size > TRANSFER_LIMIT:
        raise ValueError("仅支持不超过 128 MiB 的普通文件")
    if check_cancelled:
        check_cancelled()
    with source.open("rb") as reader:
        writer = destination.open("xb")
        try:
            with writer:
                total = 0
                while chunk := reader.read(65536):
                    if check_cancelled:
                        check_cancelled()
                    total += len(chunk)
                    if total > TRANSFER_LIMIT:
                        raise ValueError("文件超过 128 MiB")
                    writer.write(chunk)
                if check_cancelled:
                    check_cancelled()
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    return destination


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

    def browse_files(self, work_dir, path=".", *, limit=500):
        """One bounded directory page; no recursive index or filesystem watcher."""
        if not WorkspaceLocation.parse(work_dir).root:
            raise ValueError("请先选择工作区")
        limit = min(2000, max(1, int(limit)))
        files = self.files(work_dir)
        entries = []
        if files:
            metadata = files.stat(path)
            if not metadata["dir"]:
                raise ValueError("目录不存在")
            root = files.location.remote_path
            relative = files.path_type(metadata["path"]).relative_to(root).as_posix()
            page = files.call("list", path, limit=limit)
            for row in page["entries"]:
                candidate = files.path_type(row["path"])
                entries.append({"name": candidate.name, "path": candidate.relative_to(root).as_posix(),
                                "is_dir": row["type"] == "directory", "size": row["size"]})
            truncated = page["truncated"]
        else:
            root, directory = _local_path(work_dir, path)
            relative = directory.relative_to(root).as_posix()
            with os.scandir(directory) as children:
                for child in children:
                    try:
                        _, candidate = _local_path(work_dir, child.path)
                        is_dir = child.is_dir()
                        if not is_dir and not child.is_file():
                            continue
                        entries.append({"name": child.name, "path": candidate.relative_to(root).as_posix(),
                                        "is_dir": is_dir, "size": child.stat().st_size})
                    except (OSError, ValueError):
                        continue
                    if len(entries) > limit:
                        break
            truncated = len(entries) > limit
            entries = entries[:limit]
        entries.sort(key=lambda row: (not row["is_dir"], row["name"].casefold()))
        return {"path": relative, "entries": entries, "truncated": truncated}

    def upload_file(self, work_dir, directory, source, *, check_cancelled=None):
        if check_cancelled:
            check_cancelled()
        source = Path(source)
        if not source.is_file() or source.stat().st_size > TRANSFER_LIMIT:
            raise ValueError("仅支持不超过 128 MiB 的普通文件；文件夹请先打包")
        if not WorkspaceLocation.parse(work_dir).root:
            raise ValueError("请先选择工作区")
        files = self.files(work_dir)
        if files:
            folder = files.stat(directory)
            if not folder["dir"]:
                raise ValueError("目标目录不存在")
            target = files.path_type(folder["path"]) / source.name
            relative = target.relative_to(files.location.remote_path).as_posix()
            if files.stat(target)["exists"]:
                raise FileExistsError(f"已存在同名文件，请先重命名：{source.name}")
            with source.open("rb") as reader:
                data = reader.read(TRANSFER_LIMIT + 1)
            files.write_bytes(target, data, expected="", check_cancelled=check_cancelled)
        else:
            root, folder = _local_path(work_dir, directory)
            if not folder.is_dir():
                raise ValueError("目标目录不存在")
            target = folder / source.name
            _copy_file(source, target, check_cancelled)
            relative = target.relative_to(root).as_posix()
        return relative

    def prepare_file_download(self, conversation, path, *, check_cancelled=None):
        if check_cancelled:
            check_cancelled()
        if WorkspaceLocation.parse(conversation.work_dir).is_remote:
            return self.materialize(conversation, path, check_cancelled=check_cancelled)
        _, candidate = _local_path(conversation.work_dir, path)
        if not candidate.is_file() or candidate.stat().st_size > TRANSFER_LIMIT:
            raise ValueError("仅支持不超过 128 MiB 的普通文件；文件夹请先打包")
        return candidate

    def download_file(self, conversation, path, destination, *, into_directory=False, check_cancelled=None):
        source = self.prepare_file_download(conversation, path, check_cancelled=check_cancelled)
        destination = Path(destination) / source.name if into_directory else Path(destination)
        return _copy_file(source, destination, check_cancelled)

    def materialize(self, conversation, path, *, expected="", files=None, max_bytes=TRANSFER_LIMIT,
                    check_cancelled=None):
        if check_cancelled:
            check_cancelled()
        files = files or self.files(conversation.work_dir)
        metadata = files.stat(path, digest=True)
        if not metadata["file"] or metadata["size"] > max_bytes:
            raise ValueError("Remote file exceeds the supported preview/read limit")
        if expected and metadata["digest"] != expected:
            raise ValueError("内容已变化，引用的历史版本不可用；请重新核实来源。")
        # Disposable, digest-keyed views under the existing session owner; no second content store.
        name = safe_filename(files.absolute(path).name, "content")
        target = resolve_session_root(conversation.work_dir, conversation.id, data_dir=self.data_dir) / "remote-view" / metadata["digest"][:16] / name
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == metadata["digest"]:
            return target
        data = files.read_bytes(path, max_bytes=max_bytes, check_cancelled=check_cancelled)
        if hashlib.sha256(data).hexdigest() != metadata["digest"]:
            raise ValueError("Remote file changed during download")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".download-")
        try:
            with os.fdopen(descriptor, "wb") as writer:
                writer.write(data)
            if check_cancelled:
                check_cancelled()
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return target

    def close(self):
        with self._lock:
            self._closed = True
            for event in self._pending:
                event.set()
            connections, self._connections = list(self._connections.values()), {}
        for connection in connections:
            connection.close()
