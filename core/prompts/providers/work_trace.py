from __future__ import annotations

from core.prompts.providers.base import MessageProviderMixin, ProviderContext, context_item


class WorkTraceProvider(MessageProviderMixin):
    name = "work_trace"
    priority = 28

    def build_items(self, context: ProviderContext):
        try:
            state = context.conversation.get_state()
            content = state.work_trace.to_prompt_view(goal=context.latest_user_query)
        except Exception:
            content = ""
        if not content:
            return []
        return [
            context_item(
                content,
                kind=self.name,
                priority=self.priority,
                item_id="work_trace:current",
                source_ref="SessionState.work_trace",
            )
        ]
