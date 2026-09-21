"""HTTP attachment to an existing host; never creates a second application owner."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit
import uuid

import httpx

from pycat.core.app.serialization import json_value
from pycat.models.contracts.agent import ApplicationError


class RemoteClient:
    def __init__(self, endpoint, *, token=None, source='cli', transport=None):
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
            raise ValueError('Use an http:// or https:// host endpoint.')
        token = token or parse_qs(parsed.fragment).get('token', [''])[0] or os.environ.get('PYCAT_TOKEN', '')
        if not token:
            raise ValueError('Set PYCAT_TOKEN, or include #token=… in the endpoint URL.')
        self.source = source
        self.http = httpx.AsyncClient(base_url=urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', '')),
            headers={'Authorization': 'Bearer ' + token}, timeout=httpx.Timeout(30, read=None), transport=transport)

    async def request(self, method, path, **kwargs):
        try:
            response = await self.http.request(method, '/api' + path, headers={'X-Pycat-Client': self.source}, **kwargs)
        except httpx.HTTPError as exc:
            raise ApplicationError(f'Host connection failed: {exc}') from exc
        self.check(response)
        return response

    @staticmethod
    def check(response):
        if response.is_error:
            try:
                value = response.json()
                detail = value.get('error') or value.get('detail')
            except ValueError:
                detail = ''
            raise ApplicationError(str(detail or f'Host returned HTTP {response.status_code}'))

    async def bootstrap(self):
        return (await self.request('GET', '/bootstrap')).json()

    async def operation(self, name, arguments):
        if name == 'sessions.import':
            path = Path(arguments['path']).expanduser()
            return (await self.request('POST', '/import', params={'name': path.name}, content=await self._file(path))).json()
        if name == 'sessions.export':
            from pycat.core.content.export import document_format
            path = Path(arguments['destination']).expanduser()
            format = document_format(str(path), arguments.get('format'))
            response = await self.request('GET', f'/sessions/{arguments["session"]}/export', params={'format': format})
            await asyncio.to_thread(path.write_bytes, response.content)
            return {'path': str(path.resolve())}
        return (await self.request('POST', '/operations/' + name, json=arguments)).json()

    @staticmethod
    async def _file(path):
        if path.stat().st_size > 25 * 1024 * 1024:
            raise ValueError('Files must be at most 25 MiB.')
        return await asyncio.to_thread(path.read_bytes)

    async def submit(self, request, *, request_id):
        if request.attachments:
            session = request.conversation_id
            if not session:
                session = (await self.operation('sessions.create', {'work_dir': request.work_dir or ''}))['id']
            references = list(request.references)
            for item in request.attachments:
                if 'path' not in item:
                    raise ValueError('Remote CLI attachments require a local file path.')
                path = Path(item['path']).expanduser()
                response = await self.request('POST', f'/sessions/{session}/attachments',
                    params={'name': path.name}, content=await self._file(path))
                references.extend(ref['ref'] for ref in response.json()['refs'])
            request = replace(request, conversation_id=session, attachments=(), references=tuple(references))
        return (await self.request('POST', '/input', json={**json_value(request), 'request_id': request_id})).json()

    async def snapshot(self, run_id):
        return (await self.request('GET', f'/runs/{run_id}')).json()

    async def cancel(self, run_id):
        return (await self.request('POST', f'/runs/{run_id}/cancel')).json()

    async def guidance(self, run_id, text):
        return (await self.request('POST', f'/runs/{run_id}/guidance', json={'text': text})).json()

    async def respond(self, run_id, interaction_id, decision):
        return (await self.request('POST', f'/runs/{run_id}/interactions/{interaction_id}', json=decision)).json()

    async def events(self, run_id, *, after=0):
        cursor, failures = after, 0
        while True:
            try:
                async with self.http.stream('GET', f'/api/runs/{run_id}/events', params={'after': cursor}) as response:
                    if response.is_error:
                        await response.aread()
                        self.check(response)
                    data = []
                    async for line in response.aiter_lines():
                        if line.startswith('data: '):
                            data.append(line[6:])
                        elif not line and data:
                            value = json.loads('\n'.join(data)); data.clear()
                            cursor, failures = value.get('cursor', cursor), 0
                            yield value
                            if value['type'] == 'final' or value['type'] == 'reset' and value['done']:
                                return
                state = await self.snapshot(run_id)
                if state['done']:
                    yield {**state['final'], 'run_id': run_id, 'cursor': state['cursor']}
                    return
            except (httpx.TransportError, httpx.StreamError) as exc:
                failures += 1
                if failures > 5:
                    raise ApplicationError(f'Host stream disconnected: {exc}') from exc
                await asyncio.sleep(min(5, .25 * 2 ** failures))

    async def aclose(self):
        await self.http.aclose()


class RemoteExecutor:
    def __init__(self, endpoint, output):
        self.client = RemoteClient(endpoint)
        self.out = output
        self._catalog = {}

    async def connect(self):
        self._catalog = (await self.client.bootstrap())['operations']

    def catalog(self):
        return self._catalog

    def approval(self, out):
        return None

    async def execute(self, name, arguments, **_):
        result = await self.client.operation(name, arguments)
        if isinstance(result, dict) and result.get('run_id'):
            final = await self.consume(result['run_id'])
            if final['status'] != 'completed':
                raise ApplicationError(final.get('error') or final['status'])
            return final.get('data')
        return result

    async def consume(self, run_id):
        from pycat.cli.executor import CliExecutor
        from pycat.core.tools.base import ToolApprovalRequest
        from pycat.models.contracts.agent import RunEvent, RunEventKind
        async def answer(item):
            if item['kind'] == 'approval':
                decision = await CliExecutor.approval(self.out)(ToolApprovalRequest(**item['payload']))
                payload = {'approved': decision.approved, 'read_scope': decision.read_scope}
            else:
                payload = await CliExecutor.questions(self.out)(item['payload'])
                payload.pop('reason', None)
            await self.client.respond(run_id, item['id'], payload)
        try:
            async for value in self.client.events(run_id):
                if value['type'] == 'interaction':
                    await answer(value)
                elif value['type'] == 'event':
                    fields = {key: item for key, item in value.items() if key not in {'type', 'version', 'cursor'}}
                    fields['kind'] = RunEventKind(fields['kind'])
                    self.out.event(RunEvent(**fields))
                elif value['type'] == 'final':
                    return value
                elif value['type'] == 'reset':
                    for item in value['pending']:
                        await answer(item)
                    if value['done']:
                        return value['final']
            raise ApplicationError('Host stream ended without a result.')
        except asyncio.CancelledError:
            await self.client.cancel(run_id)
            raise

    async def run_once(self, request, output=None):
        result = await self.client.submit(request, request_id=uuid.uuid4().hex)
        if result.get('run_id'):
            try:
                final = await self.consume(result['run_id'])
            except asyncio.CancelledError:
                self.out.final(status='cancelled', run_id=result['run_id'], stop_reason='cancelled')
                return 130
            self.out.final(status=final['status'], message=final.get('message', ''), error=final.get('error', ''),
                conversation_id=final.get('conversation_id', ''), run_id=result['run_id'], stop_reason=final.get('stop_reason', ''),
                deliveries=final.get('deliveries', []))
            return 0 if final['status'] == 'completed' else 1
        if result.get('panel'):
            name = {'resume': 'sessions.list', 'model': 'model.list', 'mode': 'mode.list', 'agents': 'mode.list',
                    'config': 'config.read', 'mcp': 'mcp.list', 'channels': 'channels.list', 'doctor': 'doctor',
                    'status': 'doctor', 'context': 'sessions.read', 'permissions': 'sessions.settings'}.get(result['panel'])
            if not name:
                raise ValueError('Use the interactive panel for ' + result['panel'])
            self.out.result(await self.execute(name, {'session': result['session']} if name in {'sessions.read', 'sessions.settings'} else {}))
        else:
            self.out.final(status='completed', message=result.get('message', ''), conversation_id=result.get('session', ''))
        return 0

    async def aclose(self):
        await self.client.aclose()
