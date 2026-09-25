"""One durable handoff attached to its ordinary target conversation."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from pycat.models.contracts.tooling import TOOL_CATEGORIES, FilesystemScope, ToolPermissionConfig, ToolPolicy


@dataclass
class TaskAccess:
    # Explicit grants pin the catalog as well as its permissions. Newly installed
    # tools do not become available to an already delegated task by accident.
    tools: dict[str, dict[str, str]] = field(default_factory=dict)
    filesystem_mode: str = "confined"
    allow_home_read: bool = False
    work_dir: str = ""
    max_turns: int = 200

    def permissions_for(self, current: ToolPermissionConfig) -> ToolPermissionConfig:
        order = {'deny': 0, 'ask': 1, 'allow': 2}
        rules = {}
        for name, grant in self.tools.items():
            action = current.resolve(name, grant['category']).action
            rules[name] = ToolPolicy(min((action, grant['action']), key=order.__getitem__))
        return ToolPermissionConfig(category_defaults={name: ToolPolicy('deny') for name in TOOL_CATEGORIES}, tools=rules)

    def scope_for(self, current: FilesystemScope) -> FilesystemScope:
        return FilesystemScope(
            mode='full_access' if current.is_full_access and self.filesystem_mode == 'full_access' else 'confined',
            allow_home_read=current.allow_home_read and self.allow_home_read,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> TaskAccess:
        return cls(**{key: value for key, value in data.items() if key in cls.__dataclass_fields__})


@dataclass
class TaskDelegation:
    dispatch_id: str
    source_conversation_id: str
    source_message_id: str
    source_run_id: str
    fingerprint: str
    prompt: str
    access: TaskAccess
    run_id: str
    submission: str = 'queued'  # queued / started / settled; result status belongs to the saved message
    result_message_id: str = ''
    cancel_requested: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> TaskDelegation:
        values = {key: value for key, value in data.items() if key in cls.__dataclass_fields__}
        values['access'] = TaskAccess.from_dict(values['access'])
        if values.get('submission', 'queued') not in {'queued', 'started', 'settled'}:
            raise ValueError('Invalid task submission state')
        return cls(**values)


def task_projection(data: dict) -> dict | None:
    """Small, rebuildable index projection; never an execution request."""
    task = data.get('delegation')
    if not task:
        return None
    result = next((m for m in data.get('messages', []) if m.get('id') == task.get('result_message_id')), None)
    status = {'queued': 'queued', 'started': 'interrupted', 'settled': 'interrupted'}[task['submission']]
    if result:
        status = result.get('metadata', {}).get('run_status', 'interrupted')
    elif task.get('cancel_requested'):
        status = 'cancelled'
    return {'id': data['id'], 'title': data.get('title', ''), 'source_conversation_id': task['source_conversation_id'],
            'source_message_id': task['source_message_id'], 'run_id': task['run_id'],
            'submission': task['submission'], 'status': status,
            'result_message_id': task.get('result_message_id', '')}
