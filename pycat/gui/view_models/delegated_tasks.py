"""Small navigation projections; execution state stays with its existing owner."""
from dataclasses import dataclass

from pycat.models.conversation import Conversation


@dataclass(frozen=True)
class DelegatedTaskView:
    kind: str
    id: str
    title: str
    status: str
    conversation_id: str
    message_id: str = ''
    tool_call_id: str = ''

    @property
    def key(self):
        return self.kind, self.id


def independent_task_views(cards: list[dict]) -> list[DelegatedTaskView]:
    return [DelegatedTaskView('independent', card['id'], card['title'], card['status'],
                             card['id'], card.get('result_message_id', '')) for card in cards]


def subtask_views(conversation: Conversation, *, request_id: str = '') -> list[DelegatedTaskView]:
    """Read only top-level trace metadata, without copying child transcripts."""
    tasks = []
    for message in reversed(conversation.messages):
        for call in message.tool_calls or ():
            result = call.get('result')
            trace = result.get('run') if isinstance(result, dict) else None
            if not isinstance(trace, dict) or not trace.get('id'):
                continue
            status = trace.get('status', 'interrupted')
            if status == 'running' and (not request_id or message.metadata.get('request_id') != request_id or message.metadata.get('run_status') in {
                    'completed', 'cancelled', 'interrupted', 'failed'}):
                status = 'interrupted'
            tasks.append(DelegatedTaskView('subagent', str(trace['id']),
                str(trace.get('goal') or trace.get('title') or trace.get('name') or '子任务'),
                status, conversation.id, message.id, str(call.get('id') or '')))
    return tasks
