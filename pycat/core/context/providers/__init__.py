from __future__ import annotations

from pycat.core.context.providers.artifact import ArtifactProvider
from pycat.core.context.providers.archive import ArchiveIndexProvider
from pycat.core.context.providers.base import ContextProvider, ProviderContext
from pycat.core.context.providers.environment import EnvironmentProvider
from pycat.core.context.providers.memory import MemoryProvider
from pycat.core.context.providers.summary import SummaryProvider
from pycat.core.context.providers.todo import TodoProvider
from pycat.core.context.providers.work_trace import WorkTraceProvider


DEFAULT_CONTEXT_PROVIDERS: tuple[ContextProvider, ...] = (
    EnvironmentProvider(),
    SummaryProvider(),
    TodoProvider(),
    WorkTraceProvider(),
    MemoryProvider(),
    ArtifactProvider(),
    ArchiveIndexProvider(),
)


def get_default_context_providers() -> list[ContextProvider]:
    return sorted(DEFAULT_CONTEXT_PROVIDERS, key=lambda provider: provider.priority)


__all__ = [
    "ArchiveIndexProvider",
    "ArtifactProvider",
    "ContextProvider",
    "EnvironmentProvider",
    "MemoryProvider",
    "ProviderContext",
    "SummaryProvider",
    "TodoProvider",
    "WorkTraceProvider",
    "get_default_context_providers",
]
