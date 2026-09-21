"""Private OAuth credentials; never part of provider/configuration exports."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import tempfile


def _windows_protect(raw: bytes, *, decrypt: bool = False) -> bytes:
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError('Windows credential protection failed')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


class ProviderCredentialsRepository:
    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir) / 'credentials'

    def _path(self, provider_id):
        return self.root / (hashlib.sha256(str(provider_id).encode()).hexdigest() + '.oauth')

    def load(self, provider_id) -> dict | None:
        try:
            path = self._path(provider_id)
            if path.stat().st_size > 128 * 1024:
                raise ValueError('Credential file is too large')
            raw = path.read_bytes()
            if os.name == 'nt':
                raw = _windows_protect(raw, decrypt=True)
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError('Invalid credential file')
            return value
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise RuntimeError('无法读取登录凭据，请退出后重新登录。') from exc

    def save(self, provider_id, credentials):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != 'nt':
            self.root.chmod(0o700)
        raw = json.dumps(credentials).encode()
        if os.name == 'nt':
            raw = _windows_protect(raw)
        fd, name = tempfile.mkstemp(prefix='.oauth-', dir=self.root)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(raw)
            os.replace(name, self._path(provider_id))
        finally:
            Path(name).unlink(missing_ok=True)

    def delete(self, provider_id):
        self._path(provider_id).unlink(missing_ok=True)
