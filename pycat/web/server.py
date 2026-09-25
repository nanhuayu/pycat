"""Authenticated single-owner HTTP host. No independent agent or persistence path."""
from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.background import BackgroundTask
from starlette.middleware.trustedhost import TrustedHostMiddleware

from pycat.core.app.container import AppContainer
from pycat.core.app.serialization import json_value
from pycat.core.content.resolver import SessionContentResolver
from pycat.models.contracts.agent import ApplicationError, ConversationBusyError, MentionRef, RunRequest, TurnRevision


class MentionInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    kind: str = Field(max_length=20)
    id: str = Field(max_length=256)


class AttachmentInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    path: str
    name: str = ''
    mime: str = ''


class RevisionInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    action: str
    message_id: str


class InputPayload(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    request_id: str = Field(min_length=1, max_length=128)
    text: str = Field(max_length=262144)
    conversation_id: str | None = None
    work_dir: str | None = None
    model: str | None = None
    mode: str | None = None
    tool_approval: str | None = None
    filesystem_mode: str | None = None
    expected_revision: str | None = None
    attachments: list[AttachmentInput] = Field(default_factory=list, max_length=32)
    references: list[str] = Field(default_factory=list, max_length=32)
    mentions: list[MentionInput] = Field(default_factory=list, max_length=32)
    revision: RevisionInput | None = None

    def run_request(self):
        data = self.model_dump(exclude={'request_id', 'mentions', 'attachments', 'references', 'revision'})
        if any(not item.path.startswith('data:') for item in self.attachments):
            raise ValueError('Upload local files before adding them to an HTTP request.')
        return RunRequest(**data, attachments=tuple(item.model_dump() for item in self.attachments),
                          references=tuple(self.references), mentions=tuple(MentionRef(**item.model_dump()) for item in self.mentions),
                          revision=TurnRevision(**self.revision.model_dump()) if self.revision else None)


async def read_body(request, *, limit=32 * 1024 * 1024):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(413, 'Request exceeds the upload limit.')
        body.extend(chunk)
    return bytes(body)


async def read_json(request):
    try:
        value = json.loads(await read_body(request))
        if not isinstance(value, dict):
            raise ValueError('Expected a JSON object.')
        return value
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(422, str(exc)) from exc


def create_app(*, services=None, data_dir=None, token: str, allowed_hosts=None):
    if not token:
        raise ValueError('An access token is required.')

    @asynccontextmanager
    async def lifespan(app):
        container = None
        if app.state.services is None:
            container = AppContainer(data_dir=data_dir, background_curation=True)
            app.state.services = container.services
        active = app.state.services
        active.run_service.bind_loop()
        if container is not None:
            channels = active.channel_service.runtime_channels(active.settings_update_service.load())
            if channels:
                await asyncio.to_thread(active.channel_gateway.start, channels)
        try:
            yield
        finally:
            if container is not None:
                await container.aclose()

    app = FastAPI(title='PyCat', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.services = services
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or ['localhost', '127.0.0.1', '[::1]', 'testserver'])

    @app.middleware('http')
    async def boundary(request, call_next):
        if request.url.path.startswith('/api/'):
            supplied = request.headers.get('authorization', '')
            if not hmac.compare_digest(supplied, 'Bearer ' + token):
                return JSONResponse({'error': 'Authentication required.'}, status_code=401)
            origin = request.headers.get('origin')
            if origin:
                parsed = urlsplit(origin)
                if parsed.scheme not in {'http', 'https'} or parsed.netloc != request.headers.get('host'):
                    return JSONResponse({'error': 'Origin is not allowed.'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store' if request.url.path.startswith('/api/') else 'no-cache'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'"
        return response

    @app.exception_handler(ApplicationError)
    async def application_error(request, error):
        return JSONResponse({'error': str(error)}, status_code=409 if isinstance(error, ConversationBusyError) else 422)

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({'error': str(error)}, status_code=422)

    @app.exception_handler(OSError)
    async def filesystem_error(request, error):
        return JSONResponse({'error': str(error)}, status_code=422)

    @app.get('/api/bootstrap')
    async def bootstrap():
        from pycat.core.version import __version__
        active = app.state.services
        return {'version': __version__, 'protocol': 1, 'operations': active.workbench.catalog(),
                'commands': [{'name': cmd.name, 'description': cmd.description, 'aliases': cmd.aliases,
                              'usage': cmd.presentation.usage} for cmd in active.command_registry.list_commands()],
                'runs': active.interactive.list()}

    def client_source(request):
        source = request.headers.get('x-pycat-client', 'web')
        if source not in {'cli', 'tui', 'web'}:
            raise HTTPException(422, 'Unknown client source.')
        return source

    @app.post('/api/operations/{name}')
    async def operation(name: str, request: Request):
        return await app.state.services.interactive.operation(name, await read_json(request),
            request_id=request.headers.get('x-request-id', secrets.token_hex(16)), source=client_source(request))

    @app.post('/api/import')
    async def import_session(request: Request, name: str = 'session.json'):
        suffix = Path(name).suffix.lower()
        if suffix != '.json':
            raise HTTPException(422, 'Unsupported import format.')
        body = await read_body(request)
        with tempfile.TemporaryDirectory(prefix='pycat-import-') as directory:
            path = Path(directory) / ('session' + suffix)
            await asyncio.to_thread(path.write_bytes, body)
            return await app.state.services.workbench.execute('sessions.import', {'path': str(path)})

    @app.post('/api/skills/import')
    async def import_skill(request: Request, name: str = 'SKILL.md', scope: str = 'global',
                           work_dir: str = '', overwrite: bool = False):
        suffix = Path(name).suffix.lower()
        if suffix not in {'.md', '.zip'} or Path(name).name != name or '\\' in name or Path(name).is_reserved():
            raise HTTPException(422, 'Import a SKILL.md file or ZIP archive.')
        body = await read_body(request, limit=25 * 1024 * 1024)
        with tempfile.TemporaryDirectory(prefix='pycat-skill-import-') as directory:
            path = Path(directory) / name
            await asyncio.to_thread(path.write_bytes, body)
            return await app.state.services.workbench.execute('skills.import', {
                'path': str(path), 'scope': scope, 'work_dir': work_dir, 'overwrite': overwrite})

    @app.get('/api/channels/login/{identity}/qr')
    async def qr(identity: str):
        from io import BytesIO

        import qrcode
        from qrcode.image.svg import SvgPathImage
        session = app.state.services.channel_service.client_login(identity)
        def render():
            stream = BytesIO()
            qrcode.make(session.qr_text, image_factory=SvgPathImage).save(stream)
            return stream.getvalue()
        return Response(await asyncio.to_thread(render), media_type='image/svg+xml')

    @app.post('/api/input')
    async def submit(request: Request):
        try:
            payload = InputPayload.model_validate(await read_json(request))
        except ValidationError as exc:
            raise HTTPException(422, exc.errors(include_context=False, include_input=False)) from exc
        source = client_source(request)
        return await app.state.services.interactive.submit(payload.run_request(), request_id=payload.request_id, source=source)

    @app.get('/api/runs/{run_id}')
    async def run_snapshot(run_id: str):
        return app.state.services.interactive.snapshot(run_id)

    @app.get('/api/runs/{run_id}/events')
    async def events(run_id: str, after: int = 0):
        host = app.state.services.interactive
        state = host.snapshot(run_id)
        if after < 0 or after > state['cursor']:
            raise HTTPException(422, 'Invalid event cursor.')

        async def stream():
            async for event in host.events(run_id, after=after):
                yield f"id: {event['cursor']}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        return StreamingResponse(stream(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})

    @app.post('/api/runs/{run_id}/cancel')
    async def cancel(run_id: str):
        return app.state.services.interactive.cancel(run_id)

    @app.post('/api/runs/{run_id}/guidance')
    async def guidance(run_id: str, request: Request):
        data = await read_json(request)
        if set(data) != {'text'} or not isinstance(data['text'], str):
            raise HTTPException(422, 'Expected guidance text.')
        return app.state.services.interactive.guidance(run_id, data['text'])

    @app.post('/api/runs/{run_id}/interactions/{interaction_id}')
    async def respond(run_id: str, interaction_id: str, request: Request):
        return app.state.services.interactive.respond(run_id, interaction_id, await read_json(request))

    @app.post('/api/sessions/{session}/attachments')
    async def upload(session: str, request: Request, name: str = 'attachment'):
        if len(name) > 255:
            raise HTTPException(422, 'Filename is too long.')
        active = app.state.services
        conversation = await asyncio.to_thread(active.command_service.require_session, session)
        content = await read_body(request)
        prepared = await asyncio.to_thread(active.content_service.prepare_inputs, conversation,
            [{'data': content, 'name': name}])
        if prepared.failures:
            await asyncio.to_thread(active.content_service.cleanup_unreferenced, conversation, prepared.created_refs)
            raise HTTPException(422, '; '.join(item.error for item in prepared.failures))
        return {'refs': json_value(prepared.refs)}

    @app.get('/api/sessions/{session}/content')
    async def content(session: str, ref: str):
        active = app.state.services
        conversation = await asyncio.to_thread(active.command_service.require_session, session)
        resolved = await asyncio.to_thread(SessionContentResolver(active.content_service).resolve_content, conversation, ref)
        return FileResponse(resolved.path, media_type=resolved.mime, filename=resolved.name)

    @app.get('/api/sessions/{session}/export')
    async def export(session: str, format: str = 'markdown'):
        from pycat.core.content.export import CONVERSATION_FORMATS
        if format not in CONVERSATION_FORMATS:
            raise HTTPException(422, 'Unknown export format.')
        directory = tempfile.TemporaryDirectory(prefix='pycat-export-')
        try:
            path = Path(directory.name) / ('session' + CONVERSATION_FORMATS[format][0])
            await asyncio.to_thread(app.state.services.conv_service.export, session, path, format=format)
            return FileResponse(path, filename=path.name, background=BackgroundTask(directory.cleanup))
        except BaseException:
            directory.cleanup()
            raise

    assets = Path(__file__).resolve().parents[1] / 'assets'
    app.mount('/assets', StaticFiles(directory=assets / 'web', check_dir=False), name='web-assets')

    @app.get('/brand.svg')
    async def brand():
        return FileResponse(assets / 'pycat.svg', media_type='image/svg+xml')

    @app.get('/')
    async def index():
        return FileResponse(assets / 'web' / 'index.html')
    return app


def serve(args):
    import threading
    import webbrowser

    import uvicorn
    token = args.token or secrets.token_urlsafe(32)
    if args.host not in {'127.0.0.1', 'localhost', '::1'} and not args.token:
        raise ValueError('A non-local host requires an explicit --token.')
    address = '127.0.0.1' if args.host in {'0.0.0.0', '::'} else args.host
    host = f'[{address}]' if ':' in address else address
    url = f'http://{host}:{args.port}/#token={token}'
    app = create_app(data_dir=getattr(args, 'data_dir', None), token=token,
                     allowed_hosts=['*'] if args.host in {'0.0.0.0', '::'} else [args.host, 'localhost', '127.0.0.1', '[::1]'])
    print(f'PyCat WebUI: {url}', flush=True)
    if not args.no_browser:
        opener = threading.Timer(1.0, webbrowser.open, args=(url,))
        opener.daemon = True
        opener.start()
    uvicorn.run(app, host=args.host, port=args.port, access_log=False, limit_concurrency=128,
                timeout_keep_alive=10, timeout_graceful_shutdown=30)
    return 0
