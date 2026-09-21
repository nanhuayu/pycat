"""Small cross-process persistence primitives for durable PyCat stores.

The lock lives beside, rather than on, the data file so writers can publish a
fully written replacement with ``os.replace``.  A process-local lock closes
the gap left by Windows byte-range locks, which do not serialize every pair of
threads in the same process reliably.
"""

from __future__ import annotations

import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:  # Unix
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - Windows path is exercised there
    fcntl = None

try:  # Windows
    import msvcrt  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - Unix path is exercised there
    msvcrt = None


_LOCK_REGISTRY_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_OPEN_DATA_DIRS: set[str] = set()


class DataDirectoryLease:
    """Non-blocking lifetime lease for one application's writable data root."""

    def __init__(self, root: Path) -> None:
        self._key = os.path.normcase(str(root.resolve()))
        self._handle = None
        with _LOCK_REGISTRY_GUARD:
            if self._key in _OPEN_DATA_DIRS:
                raise RuntimeError(f"Application data directory is already open: {root}")
            _OPEN_DATA_DIRS.add(self._key)
        try:
            root.mkdir(parents=True, exist_ok=True)
            handle = open(root / ".application.lock", "a+b")
            self._handle = handle
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self.close()
            raise RuntimeError(f"Application data directory is already open or unavailable: {root}") from exc

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        with _LOCK_REGISTRY_GUARD:
            _OPEN_DATA_DIRS.discard(self._key)
            self._key = ""


def _thread_lock_for(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.expanduser().resolve(strict=False)))
    with _LOCK_REGISTRY_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """Lock a sibling ``.lock`` file for the full read-modify-write window."""
    target = Path(path)
    lock_path = target.with_name(target.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with _thread_lock_for(lock_path):
        handle = open(lock_path, "a+b")
        locked = False
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                locked = True
            elif msvcrt is not None:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                locked = True
            yield
        finally:
            if locked and fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            elif locked and msvcrt is not None:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            handle.close()


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Durably replace ``path`` with a unique sibling temporary file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_write_text(path: Path, payload: str) -> None:
    """UTF-8 text variant of :func:`atomic_write_bytes`."""
    atomic_write_bytes(Path(path), str(payload).encode("utf-8"))


__all__ = ["atomic_write_bytes", "atomic_write_text", "exclusive_file_lock"]
