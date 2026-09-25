"""Durable one-level handoffs. Execution and results remain owned by RunService."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from copy import deepcopy

from pycat.core.agent.policy import RunPolicyBuilder
from pycat.core.context.history import is_real_user_message
from pycat.models.contracts.agent import ConversationBusyError, InvalidRequestError, PersistenceError, RunRequest
from pycat.models.contracts.delegation import TaskAccess, TaskDelegation, task_projection
from pycat.models.conversation import Message
from pycat.models.workspace import workspace_identity

logger = logging.getLogger(__name__)


class DelegationService:
    MAX_CONCURRENT = 2
    MAX_PER_REQUEST = 4

    def __init__(self, *, runs, tools, on_change):
        self.runs, self.conversations, self.tools = runs, runs.conversations, tools
        self._on_change = on_change
        self._jobs = {}
        self._lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(self.MAX_CONCURRENT)
        self._recovery = None
        self._closing = False

    def start_recovery(self):
        if self._recovery is None and not self._closing:
            self._recovery = asyncio.create_task(self._recover(), name='pycat-task-recovery')

    async def recover(self):
        self.runs.bind_loop()
        self.start_recovery()
        await asyncio.shield(self._recovery)

    async def _recover(self):
        # A failed index update must not strand a saved task. Only startup scans
        # full files; card refreshes read the ordinary conversation index.
        rows = await asyncio.to_thread(self.conversations.reconcile_index)
        for row in rows:
            task = row.get('delegation')
            if task and task['submission'] == 'queued' and task['status'] != 'cancelled':
                self._enqueue(row['id'])

    def _require(self, conversation_id):
        conv = self.conversations.load(conversation_id)
        if conv is None:
            raise InvalidRequestError('会话不存在。')
        return conv

    def _policy(self, conv):
        return RunPolicyBuilder.build(conversation=conv, app_settings=self.runs.settings.load(), source='background')

    def _capture_access(self, parent, policy, *, read_only=False):
        grants = {}
        for tool in self.tools.registry.list_tools():
            descriptor = tool.descriptor()
            action = policy.tool_permissions.resolve(descriptor.name, descriptor.category).action
            if descriptor.category == 'delegate':
                continue
            if read_only and descriptor.category not in {'read', 'web'} and descriptor.name not in {
                    'agent__complete', 'state__todo', 'state__artifact'}:
                continue
            if policy.tool_selection.allows(descriptor) and action != 'deny':
                grants[descriptor.name] = {'category': descriptor.category, 'action': action}
        return TaskAccess(tools=grants, filesystem_mode=policy.filesystem_scope.mode,
                          allow_home_read=policy.filesystem_scope.allow_home_read,
                          work_dir=parent.work_dir, max_turns=min(200, policy.max_turns))

    async def submit(self, source_conversation_id, brief, *, dispatch_id, source_message_id='',
                     source_run_id='', policy=None, read_only=False):
        brief, dispatch_id = str(brief).strip(), str(dispatch_id).strip()
        if not brief or len(brief) > 32768 or not dispatch_id or len(dispatch_id) > 256:
            raise InvalidRequestError('任务需要明确简报（最多 32768 字符）和稳定派发标识。')
        await self.recover()
        async with self._lock:
            creation = asyncio.create_task(asyncio.to_thread(self._create, source_conversation_id, brief,
                dispatch_id, source_message_id, source_run_id, policy, bool(read_only)))
            try:
                conv = await asyncio.shield(creation)
            except asyncio.CancelledError:
                # Once saved, ownership has transferred to the application.
                conv = await asyncio.shield(creation)
                if conv.delegation.submission == 'queued':
                    self._enqueue(conv.id)
                raise
            if conv.delegation.submission == 'queued' and not conv.delegation.cancel_requested:
                self._enqueue(conv.id)
            self._changed(conv)
            return self._card(conv)

    def _create(self, source_id, brief, dispatch_id, message_id, run_id, policy, read_only):
        target_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'pycat-task:{source_id}:{dispatch_id}'))
        fingerprint = hashlib.sha256(json.dumps([brief, read_only], ensure_ascii=False).encode()).hexdigest()
        previous = self.conversations.load(target_id)
        if previous is not None:
            if previous.delegation is None or previous.delegation.fingerprint != fingerprint:
                raise InvalidRequestError('同一次派发的参数发生变化，请使用新的派发标识。')
            return previous
        if self.conversations.exists(target_id):
            raise PersistenceError('任务文件无法读取；请检查保存状态后重试。')
        parent = self._require(source_id)
        if parent.delegation is not None or policy and policy.source == 'sub_task':
            raise InvalidRequestError('独立任务仅允许一级委派。')
        origin = message_id or next((m.id for m in reversed(parent.messages) if is_real_user_message(m)), dispatch_id)
        siblings = [row for row in self.list(source_id) if row['source_message_id'] == origin]
        if len(siblings) >= self.MAX_PER_REQUEST:
            raise InvalidRequestError('同一用户请求最多派发四项独立任务。')
        conv = self.conversations.create(brief.splitlines()[0][:100])
        conv.id, conv.work_dir, conv.mode = target_id, parent.work_dir, 'agent'
        conv.set_llm_config(parent.get_llm_config())
        # Copy only execution preferences, never histories, state or channel binding.
        conv.settings = {k: deepcopy(v) for k, v in parent.settings.items() if k in {
            'tool_approval', 'filesystem_mode', 'tool_selection', 'show_thinking', 'memory_enabled'}}
        access = self._capture_access(parent, policy or self._policy(parent), read_only=read_only)
        conv.delegation = TaskDelegation(dispatch_id, source_id, origin, run_id, fingerprint, brief, access, uuid.uuid4().hex)
        if not self.conversations.save(conv):
            raise PersistenceError('任务交接未确认保存，未启动执行。请用同一派发标识重试。')
        return conv

    def _enqueue(self, task_id):
        if self._closing or task_id in self._jobs:
            return
        task = asyncio.create_task(self._execute(task_id), name=f'pycat-task-{task_id}')
        self._jobs[task_id] = task

        def finished(job):
            if self._jobs.get(task_id) is job:
                self._jobs.pop(task_id, None)
            if not job.cancelled():
                job.exception()
            latest = self.conversations.load(task_id)
            if latest:
                self._changed(latest)
        task.add_done_callback(finished)

    def revalidate(self, task_id, *, activity_token):
        """Narrow authority after the execution slot and conversation claim are acquired."""
        conv = self._require(task_id)
        delegation = conv.delegation
        if delegation is None or delegation.submission != 'queued' or delegation.cancel_requested:
            raise InvalidRequestError('任务已取消或已开始，不能重复启动。')
        parent = self._require(delegation.source_conversation_id)
        current = self._capture_access(parent, self._policy(parent))
        if workspace_identity(current.work_dir) != workspace_identity(delegation.access.work_dir):
            raise InvalidRequestError('来源工作区已改变，请检查任务后重新派发。')
        old = delegation.access
        order = {'deny': 0, 'ask': 1, 'allow': 2}
        old.tools = {name: {'category': grant['category'],
                    'action': min((grant['action'], current.tools[name]['action']), key=order.__getitem__)}
                    for name, grant in old.tools.items() if name in current.tools}
        if current.filesystem_mode != 'full_access':
            old.filesystem_mode = 'confined'
        old.allow_home_read = old.allow_home_read and current.allow_home_read
        old.max_turns = min(old.max_turns, current.max_turns)
        if not self.conversations.save(conv, activity_token=activity_token):
            raise PersistenceError('无法保存重新核对后的任务权限。')
        return conv

    async def _execute(self, task_id):
        conv = None
        try:
            async with self._slots:
                conv = self._require(task_id)
                d = conv.delegation
                await self.runs.submit(RunRequest(text=d.prompt, conversation_id=conv.id, mode='agent'),
                    source='background', run_id=d.run_id, delegation_run_id=d.run_id)
        except asyncio.CancelledError:
            if not self._closing:
                self._settle_error(task_id, '已停止独立任务。', 'cancelled')
            raise
        except PersistenceError:
            logger.exception('Task %s needs persistence inspection', task_id)
        except Exception as exc:
            self._settle_error(task_id, str(exc), 'failed')

    def _settle_error(self, task_id, error, status):
        token = self.conversations.begin_lifecycle(task_id, 'delegation')
        if not token:
            return
        try:
            conv = self._require(task_id)
            d = conv.delegation
            if d is None or d.submission == 'settled':
                return
            message = Message(role='assistant', content=error,
                              metadata={'runtime_error': True, 'run_status': status, 'request_id': d.run_id})
            conv.add_message(message)
            d.result_message_id, d.submission = message.id, 'settled'
            if not self.conversations.save(conv, activity_token=token):
                logger.error('Could not save task failure %s', task_id)
        finally:
            self.conversations.end_lifecycle(task_id, token)

    async def cancel(self, task_id):
        await self.recover()
        conv = self.conversations.cancel_delegation(task_id)
        job = self._jobs.get(task_id)
        if job is not None:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        self._settle_error(task_id, '已停止独立任务。', 'cancelled')
        self._changed(conv)
        return self.get(task_id)

    async def resume(self, task_id, text='继续完成本任务。先检查已有成果与操作状态，避免重复执行。'):
        await self.recover()
        if task_id in self._jobs:
            raise ConversationBusyError('任务仍在运行。')
        token = self.conversations.begin_lifecycle(task_id, 'delegation')
        if not token:
            raise ConversationBusyError('任务会话正在处理。')
        try:
            conv = self._require(task_id)
            if conv.delegation is None:
                raise InvalidRequestError('该会话不是独立委派任务。')
            d = conv.delegation
            d.prompt, d.run_id = text.strip(), uuid.uuid4().hex
            d.submission, d.result_message_id, d.cancel_requested = 'queued', '', False
            if not d.prompt or not self.conversations.save(conv, activity_token=token):
                raise PersistenceError('无法保存任务继续请求。')
        finally:
            self.conversations.end_lifecycle(task_id, token)
        self._enqueue(task_id)
        self._changed(conv)
        return self._card(conv)

    def _card(self, conv):
        card = task_projection(conv.to_dict())
        if card is None:
            raise InvalidRequestError('该会话不是独立委派任务。')
        if card['submission'] == 'started' and conv.id in self._jobs:
            card['status'] = 'running'
        message = next((m for m in conv.messages if m.id == card['result_message_id']), None)
        card['result'] = message.content if message else ''
        return card

    def get(self, task_id):
        return self._card(self._require(task_id))

    def list(self, source_conversation_id=None):
        cards = []
        for row in self.conversations.list_all():
            card = row.get('delegation')
            if card and (source_conversation_id is None or card['source_conversation_id'] == source_conversation_id or card['id'] == source_conversation_id):
                card = dict(card)
                if card['submission'] == 'started' and card['id'] in self._jobs:
                    card['status'] = 'running'
                cards.append(card)
        return cards

    def _changed(self, conv):
        self._on_change(conv.work_dir, conv.delegation.source_conversation_id, ('delegation',))

    async def operate(self, arguments, context):
        parent = context.conversation
        if parent is None or parent.delegation or context.runtime.source == 'sub_task':
            raise InvalidRequestError('只允许来源会话执行独立委派。')
        action = arguments.get('action')
        if action == 'submit':
            origin = next((m.id for m in reversed(parent.messages) if is_real_user_message(m)), '')
            return await self.submit(parent.id, arguments.get('brief', ''), dispatch_id=f'{origin}:{context.runtime.tool_call_id}',
                source_message_id=origin,
                source_run_id=str(getattr(context.runtime.debug_trace, 'request_id', '') or ''),
                policy=context.runtime.run_policy, read_only=arguments.get('read_only', False))
        task_id = str(arguments.get('task_id') or '')
        if action == 'status' and not task_id:
            return self.list(parent.id)
        card = self.get(task_id)
        if card['source_conversation_id'] != parent.id:
            raise InvalidRequestError('只能查询或停止本会话派发的任务。')
        if action == 'cancel':
            return await self.cancel(task_id)
        if action == 'status':
            return card
        raise InvalidRequestError('不支持的任务操作。')

    async def wait_idle(self):
        while self._jobs:
            pending = tuple(task for task in self._jobs.values() if not task.done())
            if not pending:
                self._jobs.clear()
                break
            await asyncio.gather(*pending, return_exceptions=True)

    async def aclose(self):
        self._closing = True
        if self._recovery:
            await asyncio.gather(self._recovery, return_exceptions=True)
        for task in tuple(self._jobs.values()):
            task.cancel()
        await self.wait_idle()
