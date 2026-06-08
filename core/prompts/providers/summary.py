from __future__ import annotations

from core.prompts.providers.base import MessageProviderMixin, ProviderContext, context_item
from core.prompts.user_context import build_conversation_summary


class SummaryProvider(MessageProviderMixin):
    name = "summary"
    priority = 20

    def build_items(self, context: ProviderContext):
        content = build_conversation_summary(context.conversation)
        if not content:
            return []
        return [context_item(content, kind=self.name, priority=self.priority, item_id="summary", required=True)]
