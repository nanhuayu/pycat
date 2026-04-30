from __future__ import annotations

from typing import Any


__all__ = [
    "StorageService",
    "ProviderService",
    "ConversationService",
    "AgentService",
    "ContextService",
    "SkillService",
]


def __getattr__(name: str) -> Any:
    if name == "StorageService":
        from services.storage_service import StorageService

        return StorageService
    if name == "ProviderService":
        from services.provider_service import ProviderService

        return ProviderService
    if name == "ConversationService":
        from services.conversation_service import ConversationService

        return ConversationService
    if name == "AgentService":
        from services.agent_service import AgentService

        return AgentService
    if name == "ContextService":
        from services.context_service import ContextService

        return ContextService
    if name == "SkillService":
        from services.skill_service import SkillService

        return SkillService
    raise AttributeError(name)
