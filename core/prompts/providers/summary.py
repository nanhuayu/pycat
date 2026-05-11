from __future__ import annotations

from core.prompts.providers.base import ProviderContext, synthetic_context_message
from core.prompts.user_context import build_conversation_summary


class SummaryProvider:
    name = "summary"
    priority = 20

    def build(self, context: ProviderContext):
        content = build_conversation_summary(context.conversation)
        return [synthetic_context_message(content, kind=self.name)] if content else []
