from __future__ import annotations

from pycat.core.context.providers.base import MessageProviderMixin, ProviderContext, context_item
from pycat.core.memory.service import memory_enabled


class MemoryProvider(MessageProviderMixin):
    """Emit the frozen durable-memory snapshot as one high-priority context item."""

    name = "memory"
    priority = 30

    def build_items(self, context: ProviderContext) -> list:
        if not context.memory_enabled:
            return []
        prompt = (context.memory_prompt or "").strip()
        if not prompt:
            return []
        content = f"<relevant_memory>\n{prompt}\n</relevant_memory>"
        return [
            context_item(
                content=content,
                kind="memory",
                priority=self.priority,
                source_ref="MemoryStore",
            )
        ]
