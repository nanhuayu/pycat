from __future__ import annotations

from core.context.providers.base import MessageProviderMixin, ProviderContext, context_item
from core.state.work_trace import build_work_trace_prompt_view


class WorkTraceProvider(MessageProviderMixin):
    name = "work_trace"
    priority = 28

    def build_items(self, context: ProviderContext):
        try:
            state = context.conversation.get_state()
            content = build_work_trace_prompt_view(state.work_trace, goal=context.latest_user_query)
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
