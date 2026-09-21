from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path

from pycat.core.persistence import atomic_write_text
from pycat.models.contracts.mcp import McpServerConfig


logger = logging.getLogger(__name__)


class McpServerRepository:
    """File repository for MCP server configuration."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.mcp_servers_file = self.data_dir / "mcp_servers.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[McpServerConfig]:
        try:
            if not self.mcp_servers_file.exists():
                return []
            data = json.loads(self.mcp_servers_file.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError("MCP 配置根必须是数组")
            servers: list[McpServerConfig] = []
            for index, item in enumerate(data):
                if not isinstance(item, Mapping):
                    logger.warning("Skipping MCP server entry %s: expected object", index)
                    continue
                try:
                    servers.append(McpServerConfig.from_dict(item))
                except (TypeError, ValueError) as exc:
                    logger.warning("Skipping invalid MCP server entry %s: %s", index, exc)
            return servers
        except Exception as exc:
            logger.warning("Error loading MCP servers: %s", exc)
            return []

    def save(self, servers: list[McpServerConfig]) -> bool:
        try:
            if not isinstance(servers, list):
                raise ValueError("MCP 配置必须是列表")
            validated: list[McpServerConfig] = []
            for index, server in enumerate(servers):
                if not isinstance(server, McpServerConfig):
                    raise ValueError(f"MCP 配置第 {index} 项必须是 McpServerConfig")
                server.validate()
                validated.append(server)
            data = [server.to_dict() for server in validated]
            atomic_write_text(
                self.mcp_servers_file,
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            )
            return True
        except Exception as exc:
            logger.warning("Error saving MCP servers: %s", exc)
            return False
