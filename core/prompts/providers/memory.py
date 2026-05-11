from __future__ import annotations

from core.prompts.providers.base import ProviderContext, synthetic_context_message
from core.state.services.memory_service import MemoryService

DEFAULT_MEMORY_SOURCES = ("session", "workspace", "global")


def selected_memory_sources(conversation) -> tuple[str, ...]:
    settings = getattr(conversation, "settings", {}) or {}
    raw_sources = settings.get("memory_sources")
    if raw_sources is None:
        return DEFAULT_MEMORY_SOURCES
    if isinstance(raw_sources, str):
        candidates = [part.strip().lower() for part in raw_sources.split(",")]
    elif isinstance(raw_sources, (list, tuple, set)):
        candidates = [str(item).strip().lower() for item in raw_sources]
    else:
        return DEFAULT_MEMORY_SOURCES

    normalized: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if item not in MemoryService.SOURCE_OPTIONS or item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return tuple(normalized) or DEFAULT_MEMORY_SOURCES


class MemoryProvider:
    name = "memory"
    priority = 30

    def build(self, context: ProviderContext):
        try:
            content = MemoryService.build_prompt_section(
                context.conversation.get_state(),
                context.latest_user_query,
                work_dir=context.work_dir,
                sources=selected_memory_sources(context.conversation),
            )
        except Exception:
            content = ""
        return [synthetic_context_message(content, kind=self.name)] if content else []
