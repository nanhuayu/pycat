"""Bounded UTF-8 document snapshots and explicit compare-before-save."""
from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TextDocumentSnapshot:
    path: Path
    text: str
    digest: str
    size: int
    complete: bool
    bom: bool = False
    newline: str = '\n'
    mixed_newlines: bool = False
    longest_line: int = 0
    limited_reason: str = ''

    @property
    def editable(self):
        return self.complete and not self.mixed_newlines


class TextDocumentStore:
    """Native documents are bounded; a truncated preview is never a writable draft."""
    MAX_BYTES = 20 * 1024 * 1024
    PREVIEW_BYTES = 256 * 1024
    MAX_LINES = 200000
    MAX_LINE_CHARS = 100000

    def __init__(self, *, max_bytes=MAX_BYTES, preview_bytes=PREVIEW_BYTES):
        self.max_bytes, self.preview_bytes = max_bytes, preview_bytes
        self._lock = threading.RLock()

    def read(self, source):
        path = Path(source).expanduser().resolve(strict=True)
        before = path.stat()
        complete = before.st_size <= self.max_bytes
        with path.open('rb') as stream:
            raw = stream.read((self.max_bytes if complete else self.preview_bytes) + 1)
        after = path.stat()
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise ValueError('File changed while reading; reopen it')
        limit = self.max_bytes if complete else self.preview_bytes
        complete = complete and len(raw) <= limit
        raw = raw[:limit]
        bom = raw.startswith(b'\xef\xbb\xbf')
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError as exc:
            # Only an incomplete UTF-8 character at the preview boundary may be removed.
            if not complete and exc.end == len(raw) and exc.reason == 'unexpected end of data':
                text = raw[:exc.start].decode('utf-8-sig')
            else:
                raise ValueError('Text is not valid UTF-8') from exc
        crlf, lf, cr = text.count('\r\n'), text.count('\n'), text.count('\r')
        mixed = bool((crlf and lf > crlf) or cr > crlf)
        newline = '\r\n' if crlf else '\n'
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        longest = max(map(len, text.split('\n')), default=0)
        reason = 'size' if not complete else ''
        if text.count('\n') >= self.MAX_LINES or longest > self.MAX_LINE_CHARS:
            reason = 'layout'
            complete = False
            text = text[:8192] if longest > self.MAX_LINE_CHARS else text[:self.preview_bytes]
            longest = max(map(len, text.split('\n')), default=0)
        return TextDocumentSnapshot(path, text, hashlib.sha256(raw).hexdigest() if complete else '',
                                    after.st_size, complete, bom, newline, mixed, longest, reason)

    def encode(self, snapshot, text):
        """Validate and preserve the source encoding for save or draft export."""
        if not snapshot.editable:
            raise ValueError('A preview or mixed-newline document cannot overwrite its source')
        raw = (b'\xef\xbb\xbf' if snapshot.bom else b'') + text.replace('\n', snapshot.newline).encode('utf-8')
        if len(raw) > self.max_bytes:
            raise ValueError('Edited document exceeds the 20 MiB limit')
        if text.count('\n') >= self.MAX_LINES or max(map(len, text.split('\n')), default=0) > self.MAX_LINE_CHARS:
            raise ValueError('Edited document exceeds the native layout budget')
        return raw

    def save(self, snapshot, text):
        raw = self.encode(snapshot, text)
        with self._lock:
            current = self.read(snapshot.path)
            if current.digest != snapshot.digest:
                raise ValueError('File changed outside this draft; reopen before saving')
            descriptor, name = tempfile.mkstemp(prefix='.' + snapshot.path.name + '-', dir=snapshot.path.parent)
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Preserve filesystem permissions and recheck after writing the temporary file.
                os.chmod(temporary, snapshot.path.stat().st_mode)
                if self.read(snapshot.path).digest != snapshot.digest:
                    raise ValueError('File changed while saving; draft was not written')
                os.replace(temporary, snapshot.path)
            finally:
                temporary.unlink(missing_ok=True)
        return self.read(snapshot.path)
