from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.config import get_global_data_dir
from core.app.repositories.conversation import ConversationRepository
from core.app.repositories.mcp_server import McpServerRepository
from core.app.repositories.provider import ProviderRepository
from core.app.repositories.search_config import SearchConfigRepository
from core.app.repositories.settings import SettingsRepository
from core.app.repositories.workspace_session import WorkspaceSessionService


@dataclass(frozen=True)
class AppRepositories:
    """Repository bundle for one PyCat data directory."""

    data_dir: Path
    conversations: ConversationRepository
    settings: SettingsRepository
    providers: ProviderRepository
    mcp_servers: McpServerRepository
    search_config: SearchConfigRepository
    workspace_sessions: WorkspaceSessionService

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
            workspace_sessions=WorkspaceSessionService(),
        )
