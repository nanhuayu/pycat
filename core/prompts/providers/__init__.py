from __future__ import annotations

from core.prompts.providers.artifact import ArtifactProvider
from core.prompts.providers.base import ContextProvider, ProviderContext
from core.prompts.providers.environment import EnvironmentProvider
from core.prompts.providers.memory import MemoryProvider
from core.prompts.providers.summary import SummaryProvider
from core.prompts.providers.todo import TodoProvider


DEFAULT_CONTEXT_PROVIDERS: tuple[ContextProvider, ...] = (
    EnvironmentProvider(),
    SummaryProvider(),
    TodoProvider(),
    MemoryProvider(),
    ArtifactProvider(),
)


def get_default_context_providers() -> list[ContextProvider]:
    return sorted(DEFAULT_CONTEXT_PROVIDERS, key=lambda provider: provider.priority)


__all__ = [
    "ArtifactProvider",
    "ContextProvider",
    "EnvironmentProvider",
    "MemoryProvider",
    "ProviderContext",
    "SummaryProvider",
    "TodoProvider",
    "get_default_context_providers",
]
