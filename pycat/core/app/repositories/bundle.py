from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pycat.core.app.repositories.conversation import ConversationRepository
from pycat.core.app.repositories.mcp_server import McpServerRepository
from pycat.core.app.repositories.provider import ProviderRepository
from pycat.core.app.repositories.search_config import SearchConfigRepository
from pycat.core.app.repositories.settings import SettingsRepository
from pycat.core.config import get_global_data_dir


@dataclass(frozen=True)
class AppRepositories:
    """Repository bundle for one PyCat data directory."""

    data_dir: Path
    conversations: ConversationRepository
    settings: SettingsRepository
    providers: ProviderRepository
    mcp_servers: McpServerRepository
    search_config: SearchConfigRepository

    @classmethod
    def open(cls, data_dir: str | Path | None = None) -> "AppRepositories":
        root = Path(data_dir) if data_dir is not None else get_global_data_dir()
        root.mkdir(parents=True, exist_ok=True)
        return cls(
            data_dir=root,
            conversations=ConversationRepository(root),
            settings=SettingsRepository(root),
            providers=ProviderRepository(root),
            mcp_servers=McpServerRepository(root),
            search_config=SearchConfigRepository(root),
        )
