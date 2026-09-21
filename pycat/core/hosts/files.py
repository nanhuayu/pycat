"""Remote file port using the remote path flavor, without local filesystem I/O."""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path, PurePath

from pycat.core.hosts.ssh import SshConnection
from pycat.models.workspace import WorkspaceLocation


class SshFiles:
    def __init__(self, location: WorkspaceLocation, connection: SshConnection):
        self.location = location
        self.connection = connection
        self.path_type = type(location.remote_path)
        self.read_roots = (location.root,)
        self.write_roots = (location.root,)

    def absolute(self, path) -> PurePath:
        raw = str(path or ".")
        if "\0" in raw or raw.startswith("~") or (not self.location.is_windows and "\\" in raw):
            raise ValueError("Use a remote absolute path or a path relative to the workspace")
        result = self.path_type(raw)
        if self.location.is_windows and (result.drive.startswith("\\") or bool(result.drive) != bool(result.root)):
            raise ValueError("Use a drive absolute path, for example C:/project; UNC and drive-relative paths are not supported")
        return result if result.is_absolute() else self.location.remote_path / result

    def call(self, op, path=".", *, write=False, **values):
        candidate = self.absolute(path)
        roots = self.write_roots if write else self.read_roots
        root = next((r for r in roots if candidate.is_relative_to(self.path_type(r))), None)
        if root is None:
            raise ValueError("Path is outside authorized remote roots")
        return self.connection.call(op, root=root, path=str(candidate), **values)

    def canonical(self, path) -> PurePath:
        # Metadata-only resolution; content/mutation operations recheck authorized roots remotely.
        candidate = self.absolute(path)
        result = self.connection.call("stat", root=candidate.anchor, path=str(candidate))
        return self.path_type(result["path"])

    def stat(self, path, *, digest=False):
        return self.call("stat", path, digest=digest)

    def read_bytes(self, path, *, max_bytes=128 * 1024 * 1024):
        metadata = self.stat(path, digest=True)
        if not metadata["file"] or metadata["size"] > max_bytes:
            raise ValueError("Not a regular file or file exceeds transfer limit")
        data = bytearray()
        while len(data) < metadata["size"]:
            chunk = base64.b64decode(self.call("read", path, version=metadata["version"], offset=len(data), size=65536), validate=True)
            if not chunk:
                raise ValueError("Remote file ended during transfer")
            data.extend(chunk)
        if hashlib.sha256(data).hexdigest() != metadata["digest"]:
            raise ValueError("Remote file changed during transfer")
        return bytes(data)

    def write_bytes(self, path, data, *, expected=None, check_cancelled=None):
        if len(data) > 128 * 1024 * 1024:
            raise ValueError("File exceeds 128 MiB transfer limit")
        token = self.call("write_begin", path, write=True, expected=expected)["token"]
        try:
            for offset in range(0, len(data), 65536):
                if check_cancelled:
                    check_cancelled()
                self.connection.call("write_chunk", token=token, data=base64.b64encode(data[offset:offset + 65536]).decode())
            if check_cancelled:
                check_cancelled()
            return self.connection.call("write_finish", token=token, digest=hashlib.sha256(data).hexdigest())
        except BaseException:
            try:
                self.connection.call("write_abort", token=token)
            except (OSError, ValueError):
                pass  # A lost connection cleans uncommitted uploads on EOF/lease expiry.
            raise

    def download(self, path, destination: Path):
        # Consumers receive a verified local materialization, never a remote URI as Path.
        data = self.read_bytes(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return destination
