from __future__ import annotations

from core.context.providers.base import MessageProviderMixin, ProviderContext, context_item

DEFAULT_MEMORY_SOURCES = ("session", "workspace", "global")
MEMORY_SOURCE_OPTIONS = set(DEFAULT_MEMORY_SOURCES)


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
        if item not in MEMORY_SOURCE_OPTIONS or item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return tuple(normalized) or DEFAULT_MEMORY_SOURCES


class MemoryProvider(MessageProviderMixin):
    name = "memory"
    priority = 30

    def build_items(self, context: ProviderContext):
        content = str(getattr(context, "memory_prompt", "") or "")
        return [context_item(content, kind=self.name, priority=self.priority, item_id="memory:relevant", source_ref="MemoryService")] if content else []
