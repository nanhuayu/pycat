"""
MCP Server Configuration Model
"""

from dataclasses import dataclass, field
from typing import List, Dict

@dataclass
class McpServerConfig:
    """Configuration for a Stdio MCP Server"""
    name: str
    command: str
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "command": self.command,
            "args": self.args,
            "env": self.env,
            "enabled": self.enabled
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'McpServerConfig':
        return cls(
            name=data.get("name", "Unnamed"),
            command=data.get("command", ""),
            args=data.get("args", []),
            env=data.get("env", {}),
            enabled=data.get("enabled", True)
        )
