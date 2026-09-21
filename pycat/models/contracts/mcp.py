"""
MCP Server Configuration Model
"""

from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Any, List, Dict

TRANSPORT_STDIO = "stdio"
TRANSPORT_STREAMABLE_HTTP = "streamable_http"
TRANSPORT_SSE = "sse"

VALID_TRANSPORTS = (TRANSPORT_STDIO, TRANSPORT_STREAMABLE_HTTP, TRANSPORT_SSE)


@dataclass
class McpServerConfig:
    """Configuration for one MCP server connection.

    Transports:
      - ``stdio``: local subprocess started via ``command``/``args``/``env``;
      - ``streamable_http``: remote streamable HTTP endpoint via ``url``/``headers``;
      - ``sse``: legacy remote SSE endpoint via ``url``/``headers``.

    Per-tool enable / auto-approve policies are stored in the global
    ``ToolPermissionConfig`` (settings.json) so that all tools—built-in
    and MCP—are managed in one place.
    """

    name: str
    transport: str = TRANSPORT_STDIO
    # stdio-only fields
    command: str = ""
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    # http-only fields (streamable_http / sse)
    url: str = ""
    headers: Dict[str, str] = field(default_factory=dict)

    enabled: bool = True

    # Cached tool names discovered from this server (last known list).
    cached_tools: List[str] = field(default_factory=list)
    # An explicit host adapter, independent of the user-visible server name.
    integration: str = ""

    @classmethod
    def from_mcp_json(cls, name: str, raw: dict) -> 'McpServerConfig':
        """Import an ecosystem entry as a disabled, validated draft."""
        if not isinstance(name, str) or not isinstance(raw, dict):
            raise ValueError('MCP 服务名称必须是字符串，条目必须是对象')
        if '_pycat_transport' in raw:
            transport = raw['_pycat_transport']
        elif 'transport' in raw:
            transport = raw['transport']
        else:
            if str(raw.get('command') or '').strip():
                transport = TRANSPORT_STDIO
            elif str(raw.get('url') or '').strip():
                transport = TRANSPORT_STREAMABLE_HTTP
            else:
                raise ValueError('缺少 command 或 url')
        return cls.from_dict({key: value for key, value in {
            **raw, 'name': name.strip(), 'transport': transport, 'enabled': False,
            'integration': raw.get('_pycat_integration', ''),
        }.items() if key in cls.__dataclass_fields__})

    def to_mcp_json(self) -> dict:
        """Export transport configuration; preserve SSE with PyCat's extension."""
        if self.is_http_transport():
            entry = {'url': self.url}
            if self.headers:
                entry['headers'] = dict(self.headers)
            if self.normalized_transport() == TRANSPORT_SSE:
                entry['_pycat_transport'] = TRANSPORT_SSE
            return entry
        entry = {'command': self.command}
        if self.integration:
            entry['_pycat_integration'] = self.integration
        for key in ('args', 'env', 'cwd'):
            if getattr(self, key):
                entry[key] = getattr(self, key)
        return entry

    def normalized_transport(self) -> str:
        if not isinstance(self.transport, str):
            raise ValueError("MCP transport 必须是字符串")
        value = self.transport.strip().lower()
        if value in VALID_TRANSPORTS:
            return value
        raise ValueError(
            f"MCP transport 无效：{self.transport!r}；支持 {', '.join(VALID_TRANSPORTS)}"
        )

    def is_http_transport(self) -> bool:
        return self.normalized_transport() in (TRANSPORT_STREAMABLE_HTTP, TRANSPORT_SSE)

    def validate(self) -> None:
        """Raise ValueError when required fields for the transport are missing."""
        self._validate_types()
        transport = self.normalized_transport()
        if self.integration not in {"", "agent-browser"}:
            raise ValueError("未知的 MCP 集成类型")
        if self.integration and transport != TRANSPORT_STDIO:
            raise ValueError("托管浏览器必须使用 stdio")
        if not self.name.strip():
            raise ValueError("MCP 服务名称不能为空")
        if transport == TRANSPORT_STDIO:
            if not self.command.strip():
                raise ValueError(f"MCP 服务“{self.name}”缺少启动命令")
            return
        url = self.url.strip()
        if not url:
            raise ValueError(f"MCP 服务“{self.name}”缺少服务 URL")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError(f"MCP 服务“{self.name}”的 URL 必须以 http:// 或 https:// 开头")

    def endpoint_summary(self) -> str:
        """One-line human readable endpoint for list rows and tooltips."""
        if self.is_http_transport():
            return str(self.url or "").strip() or "未设置 URL"
        return str(self.command or "").strip() or "未设置命令"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "transport": self.normalized_transport(),
            "command": self.command,
            "args": self.args,
            "env": self.env,
            "cwd": self.cwd,
            "url": self.url,
            "headers": self.headers,
            "enabled": self.enabled,
            "cached_tools": list(self.cached_tools or []),
            "integration": self.integration,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "McpServerConfig":
        if not isinstance(data, Mapping):
            raise ValueError("MCP 配置必须是对象")

        def string_field(key: str, default: str) -> str:
            value = data[key] if key in data else default
            if not isinstance(value, str):
                raise ValueError(f"MCP 字段 {key!r} 必须是字符串")
            return value

        def string_list_field(key: str) -> list[str]:
            value = data[key] if key in data else []
            if not isinstance(value, list):
                raise ValueError(f"MCP 字段 {key!r} 必须是字符串数组")
            if any(not isinstance(item, str) for item in value):
                raise ValueError(f"MCP 字段 {key!r} 必须是字符串数组")
            return list(value)

        def string_mapping_field(key: str) -> dict[str, str]:
            value = data[key] if key in data else {}
            if not isinstance(value, Mapping):
                raise ValueError(f"MCP 字段 {key!r} 必须是字符串对象")
            if any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
                raise ValueError(f"MCP 字段 {key!r} 必须是字符串对象")
            return dict(value)

        enabled = data["enabled"] if "enabled" in data else True
        if not isinstance(enabled, bool):
            raise ValueError("MCP 字段 'enabled' 必须是布尔值")

        if "transport" in data:
            transport_value = data["transport"]
            if not isinstance(transport_value, str):
                raise ValueError("MCP 字段 'transport' 必须是字符串")
            transport = transport_value.strip().lower()
            if transport not in VALID_TRANSPORTS:
                raise ValueError(
                    f"MCP transport 无效：{transport_value!r}；支持 {', '.join(VALID_TRANSPORTS)}"
                )
        else:
            transport = TRANSPORT_STDIO

        config = cls(
            name=string_field("name", "Unnamed"),
            transport=transport,
            command=string_field("command", ""),
            args=string_list_field("args"),
            env=string_mapping_field("env"),
            cwd=string_field("cwd", ""),
            url=string_field("url", ""),
            headers=string_mapping_field("headers"),
            enabled=enabled,
            cached_tools=string_list_field("cached_tools"),
            integration=string_field("integration", ""),
        )
        config.validate()
        return config

    def _validate_types(self) -> None:
        for key in ("name", "command", "url", "cwd", "integration"):
            if not isinstance(getattr(self, key), str):
                raise ValueError(f"MCP 字段 {key!r} 必须是字符串")
        if not isinstance(self.args, list) or any(not isinstance(item, str) for item in self.args):
            raise ValueError("MCP 字段 'args' 必须是字符串数组")
        for key in ("env", "headers"):
            value = getattr(self, key)
            if not isinstance(value, Mapping) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()
            ):
                raise ValueError(f"MCP 字段 {key!r} 必须是字符串对象")
        if not isinstance(self.enabled, bool):
            raise ValueError("MCP 字段 'enabled' 必须是布尔值")
        if not isinstance(self.cached_tools, list) or any(
            not isinstance(item, str) for item in self.cached_tools
        ):
            raise ValueError("MCP 字段 'cached_tools' 必须是字符串数组")
