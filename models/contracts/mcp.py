"""
MCP Server Configuration Model
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class McpServerConfig:
    """Configuration for a Stdio MCP Server.

    Per-tool enable / auto-approve policies are stored in the global
    ``ToolPermissionConfig`` (settings.json) so that all tools—built-in
    and MCP—are managed in one place.
    """

    name: str
    command: str
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    enabled: bool = True

    # Cached tool names discovered from this server (last known list).
    cached_tools: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "command": self.command,
            "args": self.args,
            "env": self.env,
            "enabled": self.enabled,
            "cached_tools": list(self.cached_tools or []),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "McpServerConfig":
        return cls(
            name=data.get("name", "Unnamed"),
            command=data.get("command", ""),
            args=data.get("args", []),
            env=data.get("env", {}),
            enabled=data.get("enabled", True),
            cached_tools=list(data.get("cached_tools", []) or []),
        )
