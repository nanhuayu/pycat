from __future__ import annotations

import json
import logging
from pathlib import Path

from models.contracts.mcp import McpServerConfig


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
            return [McpServerConfig.from_dict(item) for item in data if isinstance(item, dict)]
        except Exception as exc:
            logger.warning("Error loading MCP servers: %s", exc)
            return []

    def save(self, servers: list[McpServerConfig]) -> bool:
        try:
            data = [server.to_dict() for server in servers]
            self.mcp_servers_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except Exception as exc:
            logger.warning("Error saving MCP servers: %s", exc)
            return False
