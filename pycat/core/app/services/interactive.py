"""Bounded observation and explicit interaction for attached clients.

One host consumer drains each RunHandle independently of client speed. Session
files remain canonical; replay is a short-lived projection, not another store.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

from pycat.core.app.serialization import json_value
from pycat.core.app.services.commands import CommandExecution
from pycat.core.content.references import latest_turn_deliveries
from pycat.core.tools.base import ApprovalDecision
from pycat.models.contracts.agent import InvalidRequestError, RunResult, RunStatus, RunStopReason


@dataclass
class ObservedRun:
    handle: object
    session: str = ''
    events: deque = field(default_factory=deque)
    size: int = 0
    cursor: int = 0
    pending: dict = field(default_factory=dict)
    decisions: dict = field(default_factory=dict)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    final: dict | None = None
    finished_at: float = 0


class InteractiveService:
    MAX_EVENTS = 2048
    MAX_RUN_BYTES = 8 * 1024 * 1024
    MAX_TOTAL_BYTES = 64 * 1024 * 1024
    RETAIN_SECONDS = 120

    def __init__(self, *, commands, workbench):
        self.commands, self.workbench = commands, workbench
        self._runs = {}
        self._requests = {}
        self._lock = asyncio.Lock()
        self._closed = False

    def _prune(self):
        now = time.monotonic()
        expired = {key for key, record in self._runs.items()
                   if record.finished_at and now - record.finished_at > self.RETAIN_SECONDS}
        for key in expired:
            del self._runs[key]
        self._requests = {key: value for key, value in self._requests.items()
                          if now - value[2] <= self.RETAIN_SECONDS or value[1].get('run_id') in self._runs}
        while sum(record.size for record in self._runs.values()) > self.MAX_TOTAL_BYTES:
            record = max(self._runs.values(), key=lambda item: item.size)
            if not record.events:
                break
            _, size = record.events.popleft()
            record.size -= size

    async def submit(self, request, *, request_id: str, source: str):
        async def dispatch(approve, question):
            return await self.commands.dispatch(request, source=source, approval_callback=approve, questions_callback=question)
        return await self._submit(json_value(request), request_id, dispatch)

    async def operation(self, name, arguments, *, request_id, source):
        if not self.workbench.catalog().get(name, {}).get('observed'):
            return await self.workbench.execute(name, arguments)
        async def dispatch(approve, question):
            async def execute(handle):
                value = await self.workbench.execute(name, arguments, approval_callback=approve)
                handle.output = value
                failed = isinstance(value, dict) and value.get('is_error', False)
                session = value.get('session') if isinstance(value, dict) else None
                return RunResult(status=RunStatus.FAILED if failed else RunStatus.COMPLETED,
                    stop_reason=RunStopReason.ERROR if failed else RunStopReason.COMPLETED,
                    conversation=self.commands.require_session(session) if session else None)
            return CommandExecution('run', handle=self.commands.runs.start_action(execute,
                conversation_id=arguments.get('session'), source=source))
        return await self._submit({'operation': name, 'arguments': arguments}, request_id, dispatch)

    async def _submit(self, payload, request_id, dispatch):
        if not request_id or len(request_id) > 128:
            raise InvalidRequestError('A request ID of at most 128 characters is required.')
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        async with self._lock:
            self._prune()
            if self._closed:
                raise InvalidRequestError('The application is closing.')
            if request_id in self._requests:
                previous, result, _ = self._requests[request_id]
                if previous != digest:
                    raise InvalidRequestError('The request ID was already used with different input.')
                return result
            if len(self._runs) >= 256 or sum(not item.final for item in self._runs.values()) >= 16 or len(self._requests) >= 1024:
                raise InvalidRequestError('The host is at its observation limit; retry after completed runs expire.')
            record = None

            async def approve(value):
                decision = await self._ask(record, 'approval', json_value(value))
                return ApprovalDecision(approved=bool(decision.get('approved')),
                                        read_scope=decision.get('read_scope', ''))

            async def question(value):
                return await self._ask(record, 'question', value)

            action = await dispatch(approve, question)
            result = {'kind': action.kind, 'message': action.message, 'panel': action.panel,
                      'data': json_value(action.data), 'session': action.conversation.id if action.conversation else ''}
            if action.handle:
                record = ObservedRun(action.handle, session=action.handle._conversation_id)
                self._runs[action.handle.id] = record
                result['run_id'] = action.handle.id
                result['session'] = record.session
                record.task = asyncio.create_task(self._observe(record), name=f'pycat-observe-{action.handle.id}')
            self._requests[request_id] = (digest, result, time.monotonic())
            return result

    def _publish(self, record, payload):
        record.cursor += 1
        payload = {'version': 1, **payload, 'run_id': record.handle.id, 'cursor': record.cursor}
        size = len(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
        record.events.append((payload, size))
        record.size += size
        while len(record.events) > self.MAX_EVENTS or record.size > self.MAX_RUN_BYTES:
            _, removed = record.events.popleft()
            record.size -= removed
        self._prune()
        record.changed.set()

    async def _observe(self, record):
        try:
            async for event in record.handle.events():
                record.session = event.conversation_id or record.session
                self._publish(record, {'type': 'event', **json_value(event)})
            result = await record.handle.result()
            if result.conversation:
                record.session = result.conversation.id
            final = {'type': 'final', 'status': result.status.value, 'stop_reason': result.stop_reason.value,
                     'message': result.final_message.content[:262144] if result.final_message else '', 'error': result.error or '',
                     'data': record.handle.output,
                     'deliveries': [ref.to_dict() for ref in latest_turn_deliveries(result.conversation)] if result.conversation else []}
            if final['data'] is not None:
                encoded = json.dumps(final['data'], ensure_ascii=False)
                if len(encoded) > 262144:
                    final['data'] = {'truncated': True, 'preview': encoded[:262144]}
        except asyncio.CancelledError:
            record.handle.cancel()
            await record.handle.result()
            final = {'type': 'final', 'status': 'cancelled', 'stop_reason': 'cancelled', 'message': '', 'error': ''}
        except Exception as exc:
            final = {'type': 'final', 'status': 'failed', 'stop_reason': 'error', 'message': '', 'error': str(exc)}
        finally:
            for pending in record.pending.values():
                if not pending['future'].done():
                    pending['future'].cancel()
            record.pending.clear()
        final['conversation_id'] = record.session
        record.final = final
        record.finished_at = time.monotonic()
        self._publish(record, final)

    async def _ask(self, record, kind, payload):
        if record is None:
            raise InvalidRequestError('Interaction has no owning run.')
        if len(json.dumps(payload, ensure_ascii=False)) > 262144:
            return {'approved': False} if kind == 'approval' else {'selected': [], 'freeText': None, 'skipped': True, 'reason': 'request_too_large'}
        identity = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        public = {'id': identity, 'kind': kind, 'payload': payload, 'expires_at': time.time() + 300}
        record.pending[identity] = {'public': public, 'future': future}
        self._publish(record, {'type': 'interaction', **public})
        try:
            return await asyncio.wait_for(future, timeout=300)
        except TimeoutError:
            return {'approved': False} if kind == 'approval' else {'selected': [], 'freeText': None, 'skipped': True, 'reason': 'expired'}
        finally:
            record.pending.pop(identity, None)
            self._publish(record, {'type': 'interaction_closed', 'id': identity})

    def _get(self, run_id):
        self._prune()
        if run_id not in self._runs:
            raise InvalidRequestError('Run observation expired or does not exist; reload the saved session.')
        return self._runs[run_id]

    def snapshot(self, run_id):
        record = self._get(run_id)
        return {'run_id': run_id, 'session': record.session, 'cursor': record.cursor,
                'done': record.final is not None, 'final': record.final,
                'pending': [value['public'] for value in record.pending.values()],
                'first_cursor': record.events[0][0]['cursor'] if record.events else record.cursor + 1}

    def list(self):
        self._prune()
        return [self.snapshot(key) for key in list(self._runs)]

    def respond(self, run_id, interaction_id, decision):
        record = self._get(run_id)
        if interaction_id in record.decisions:
            if record.decisions[interaction_id] == decision:
                return {'accepted': True, 'duplicate': True}
            raise InvalidRequestError('This interaction already has a different response.')
        pending = record.pending.get(interaction_id)
        if pending is None or pending['future'].done():
            raise InvalidRequestError('The interaction is no longer pending.')
        public = pending['public']
        if public['kind'] == 'approval':
            if set(decision) - {'approved', 'read_scope'} or type(decision.get('approved')) is not bool:
                raise InvalidRequestError('Invalid approval decision.')
            if decision.get('read_scope', '') not in {'', 'call', 'run'}:
                raise InvalidRequestError('Invalid read grant scope.')
        else:
            if set(decision) - {'selected', 'freeText', 'skipped'}:
                raise InvalidRequestError('Invalid question response.')
            selected = decision.get('selected', [])
            options = public['payload'].get('options', [])
            labels = {item['label'] for item in options}
            if not isinstance(selected, list) or any(not isinstance(item, str) or item not in labels for item in selected):
                raise InvalidRequestError('Select an existing option.')
            if len(set(selected)) != len(selected):
                raise InvalidRequestError('Do not repeat an option.')
            if not public['payload'].get('multiple') and len(selected) > 1:
                raise InvalidRequestError('This question accepts one option.')
            if decision.get('freeText') is not None and not isinstance(decision['freeText'], str):
                raise InvalidRequestError('Free text must be a string.')
            if len(decision.get('freeText') or '') > 262144 or type(decision.get('skipped', False)) is not bool:
                raise InvalidRequestError('Invalid question response.')
            if not selected and not (decision.get('freeText') or '').strip() and not decision.get('skipped'):
                raise InvalidRequestError('Choose an answer, enter text, or explicitly skip.')
        record.decisions[interaction_id] = dict(decision)
        if len(record.decisions) > 1024:
            del record.decisions[next(iter(record.decisions))]
        pending['future'].set_result(dict(decision))
        return {'accepted': True, 'duplicate': False}

    def cancel(self, run_id):
        record = self._get(run_id)
        if record.final is None:
            record.handle.cancel()
        return {'accepted': True, 'done': record.final is not None}

    def guidance(self, run_id, text):
        return {'accepted': self._get(run_id).handle.submit_guidance(text)}

    async def wait(self, run_id):
        await asyncio.shield(self._get(run_id).task)
        return self.snapshot(run_id)

    async def events(self, run_id, *, after=0):
        record = self._get(run_id)
        cursor = max(0, after)
        if cursor > record.cursor:
            raise InvalidRequestError('Event cursor is ahead of the run.')
        while True:
            record.changed.clear()
            first = record.events[0][0]['cursor'] if record.events else record.cursor + 1
            if cursor < first - 1:
                yield {'version': 1, 'type': 'reset', **self.snapshot(run_id)}
                cursor = record.cursor
            else:
                batch = [value for value, _ in record.events if value['cursor'] > cursor]
                for value in batch:
                    cursor = value['cursor']
                    yield value
            if record.final is not None and cursor >= record.cursor:
                return
            try:
                await asyncio.wait_for(record.changed.wait(), timeout=15)
            except TimeoutError:
                yield {'version': 1, 'type': 'heartbeat', 'run_id': run_id, 'cursor': cursor}

    async def aclose(self):
        self._closed = True
        for record in self._runs.values():
            if record.final is None:
                record.handle.cancel()
        await asyncio.gather(*(record.task for record in self._runs.values()), return_exceptions=True)
        self._runs.clear()
        self._requests.clear()
