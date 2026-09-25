"""Pure workspace identity. Remote paths never depend on the client's OS."""
from __future__ import annotations

import ntpath
import os
import posixpath
import re
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import quote, unquote, urlsplit


@dataclass(frozen=True)
class WorkspaceLocation:
    root: str = ""
    host: str = ""
    port: int | None = None

    @property
    def is_remote(self) -> bool:
        return bool(self.host)

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}" if self.port is not None else self.host

    @property
    def value(self) -> str:
        path = "/" + self.root if self.is_windows else self.root
        return f"ssh://{self.endpoint}{quote(path, safe='/')}" if self.host else self.root

    @property
    def is_windows(self) -> bool:
        return self.is_remote and bool(re.match(r"^[A-Za-z]:/", self.root))

    @property
    def remote_path(self) -> PurePosixPath | PureWindowsPath:
        return PureWindowsPath(self.root) if self.is_windows else PurePosixPath(self.root)

    @property
    def label(self) -> str:
        name = posixpath.basename(self.root.replace("\\", "/").rstrip("/")) or self.root
        return f"{name} · {self.endpoint}" if self.host else name

    @classmethod
    def ssh(cls, host: str, root: str, *, port: int | None = None) -> WorkspaceLocation:
        host, root = str(host).strip(), str(root).strip()
        # Omitted ports inherit OpenSSH config; an explicit port is part of identity.
        if port is not None and (type(port) is not int or not 1 <= port <= 65535):
            raise ValueError("SSH 端口必须是 1–65535 的整数；留空沿用 SSH 配置")
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*", host) or host.count("@") > 1:
            raise ValueError("SSH 主机请填写配置别名或 user@host；端口请填写右侧端口栏")
        if any(ord(c) < 32 for c in root):
            raise ValueError("SSH 目录不能包含控制字符")
        if re.match(r"^/[A-Za-z]:/", root):
            root = root[1:]  # URI drive paths use a leading slash.
        if re.match(r"^[A-Za-z]:[/\\]", root):
            root = ntpath.normpath(root).replace("\\", "/")
            return cls(root[0].upper() + root[1:], host, port)
        if not root.startswith("/") or root.startswith("//") or "\\" in root:
            raise ValueError("SSH 目录必须是绝对路径，例如 /home/ubuntu/project 或 C:/Users/HP/project")
        return cls(posixpath.normpath(root), host, port)

    @classmethod
    def parse(cls, value: str | None) -> WorkspaceLocation:
        raw = str(value or "").strip()
        if raw.startswith("ssh://"):
            parsed = urlsplit(raw)
            if parsed.query or parsed.fragment:
                raise ValueError("SSH 工作区地址不能包含查询或片段")
            host = parsed.netloc
            port = None
            if ":" in host:
                host, port_text = host.rsplit(":", 1)
                if not re.fullmatch(r"[0-9]+", port_text):
                    raise ValueError("SSH 端口必须是 1–65535 的整数")
                port = int(port_text)
            return cls.ssh(host, unquote(parsed.path), port=port)
        return cls("" if raw in ("", ".") else raw)


def workspace_identity(value: str) -> str:
    """Lexical host/path identity, preserving Linux path case on Windows clients."""
    location = WorkspaceLocation.parse(value)
    if location.is_remote:
        return WorkspaceLocation.ssh(location.host, location.root.casefold(), port=location.port).value if location.is_windows else location.value
    return os.path.normcase(os.path.abspath(os.path.expanduser(location.root))) if location.root else ""
