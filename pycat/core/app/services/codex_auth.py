"""App-owned ChatGPT PKCE login and refresh; no Agent loop or Codex cache access."""
from __future__ import annotations

import asyncio
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import secrets
import threading
import time
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from pycat.core.app.repositories.provider_credentials import ProviderCredentialsRepository

CLIENT_ID = 'app_EMoamEEZ73f0CkXaXp7hrann'
AUTH_URL = 'https://auth.openai.com/oauth/authorize'
TOKEN_URL = 'https://auth.openai.com/oauth/token'


class _LoopbackServer(HTTPServer):
    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(2)
        return connection, address


class CodexLogin:
    def __init__(self, provider_id, *, port=1455):
        self.provider_id = provider_id
        self.cancelled = threading.Event()
        self.state = secrets.token_urlsafe(32)
        self.verifier = secrets.token_urlsafe(48)
        self.code = ''
        self.error = False
        flow = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # OAuth callback URLs contain authorization codes.

            def do_GET(self):
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                valid = (
                    parsed.path == '/auth/callback' and len(self.path) < 16384
                    and secrets.compare_digest(query.get('state', [''])[0], flow.state)
                    and not flow.cancelled.is_set() and not flow.code
                )
                if valid:
                    flow.error = bool(query.get('error'))
                    flow.code = query.get('code', [''])[0]
                success = valid and bool(flow.code or flow.error)
                self.send_response(200 if success else 400)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(('已收到授权结果，请返回 PyCat。' if success else '无效的登录回调。').encode())

        try:
            self.server = _LoopbackServer(('127.0.0.1', port), Handler)
        except OSError as exc:
            raise RuntimeError('无法监听本地登录端口 1455，请关闭其它等待登录的窗口后重试。') from exc
        self.server.timeout = 0.2
        actual_port = self.server.server_address[1]
        self.redirect_uri = f'http://localhost:{actual_port}/auth/callback'
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip('=')
        self.authorization_url = AUTH_URL + '?' + urlencode({
            'response_type': 'code', 'client_id': CLIENT_ID, 'redirect_uri': self.redirect_uri,
            'scope': 'openid profile email offline_access', 'state': self.state,
            'code_challenge': challenge, 'code_challenge_method': 'S256',
            'id_token_add_organizations': 'true', 'codex_cli_simplified_flow': 'true',
        })

    def cancel(self):
        self.cancelled.set()
        self.server.server_close()


class CodexAuthService:
    def __init__(self, repository: ProviderCredentialsRepository, *, transport_factory=None):
        self._repository = repository
        self._transport_factory = transport_factory
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._flows = {}
        self._generation = {}

    def begin_login(self, provider_id, *, port=1455):
        with self._lock:
            previous = self._flows.pop(provider_id, None)
            if previous:
                previous.cancel()
            flow = CodexLogin(provider_id, port=port)
            self._flows[provider_id] = flow
            return flow

    def pending_login(self, provider_id):
        with self._lock:
            return self._flows.get(provider_id)

    def _exchange(self, fields):
        try:
            with httpx.Client(timeout=30, transport=self._transport_factory() if self._transport_factory else None) as client:
                response = client.post(TOKEN_URL, data={'client_id': CLIENT_ID, **fields})
            if response.status_code != 200:
                error = RuntimeError(f'登录授权失败（HTTP {response.status_code}），请重新登录。')
                error.invalid_credentials = response.status_code in {400, 401, 403}
                raise error
            result = response.json()
            access = str(result.get('access_token') or '')
            refresh = str(result.get('refresh_token') or fields.get('refresh_token') or '')
            expires = float(result.get('expires_in') or 0)
            if not access or not refresh or not math.isfinite(expires) or expires <= 0:
                raise ValueError('invalid tokens')
            payload = json.loads(base64.urlsafe_b64decode(access.split('.')[1] + '==='))
            account = str(payload.get('https://api.openai.com/auth', {}).get('chatgpt_account_id') or '')
            if not account or any(ch in account + access for ch in '\r\n'):
                raise ValueError('missing account')
            return {'access_token': access, 'refresh_token': refresh, 'expires_at': time.time() + expires,
                    'account_id': account, 'email': str(payload.get('email') or '')}
        except httpx.HTTPError as exc:
            raise RuntimeError('无法连接登录服务，请检查网络后重试。') from exc
        except (ValueError, TypeError, IndexError, AttributeError) as exc:
            raise RuntimeError('登录服务返回了不完整的凭据，请重新登录。') from exc

    def prepare_login(self, flow):
        # The PKCE URL is local; account editors share the two-stage lifecycle.
        return flow

    def finish_login(self, flow, *, timeout=180):
        deadline = time.monotonic() + timeout
        try:
            while not flow.code and not flow.error and not flow.cancelled.is_set():
                if time.monotonic() >= deadline:
                    raise RuntimeError('登录等待超时，请重试。')
                try:
                    flow.server.handle_request()
                except (OSError, ValueError):
                    if not flow.cancelled.is_set():
                        raise
            if flow.cancelled.is_set():
                raise RuntimeError('登录已取消。')
            if flow.error:
                raise RuntimeError('登录未授权，请重试。')
            credentials = self._exchange({'grant_type': 'authorization_code', 'code': flow.code,
                                          'code_verifier': flow.verifier, 'redirect_uri': flow.redirect_uri})
            with self._lock:
                if flow.cancelled.is_set() or self._flows.get(flow.provider_id) is not flow:
                    raise RuntimeError('登录已取消。')
                self._repository.save(flow.provider_id, credentials)
                return self.status(flow.provider_id)
        finally:
            flow.server.server_close()
            with self._lock:
                if self._flows.get(flow.provider_id) is flow:
                    self._flows.pop(flow.provider_id)

    def status(self, provider_id):
        with self._lock:
            value = self._repository.load(provider_id)
            return {'connected': bool(value), 'email': str((value or {}).get('email') or '')}

    def logout(self, provider_id):
        with self._lock:
            self._generation[provider_id] = self._generation.get(provider_id, 0) + 1
            flow = self._flows.pop(provider_id, None)
            if flow:
                flow.cancel()
            self._repository.delete(provider_id)

    def close(self):
        with self._lock:
            for flow in self._flows.values():
                flow.cancel()
            self._flows.clear()

    async def headers(self, provider, model_id=''):
        return await asyncio.to_thread(self._headers_sync, provider, model_id)

    def _headers_sync(self, provider, model_id):
        with self._refresh_lock:
            with self._lock:
                generation = self._generation.get(provider.id, 0)
                value = self._repository.load(provider.id)
            if not value:
                raise RuntimeError('请先在模型与服务中登录 ChatGPT。')
            if float(value.get('expires_at', 0)) <= time.time() + 120:
                try:
                    refreshed = self._exchange({'grant_type': 'refresh_token', 'refresh_token': value['refresh_token']})
                except RuntimeError as exc:
                    if getattr(exc, 'invalid_credentials', False):
                        with self._lock:
                            if generation == self._generation.get(provider.id, 0):
                                self._repository.delete(provider.id)
                    raise
                with self._lock:
                    if generation != self._generation.get(provider.id, 0):
                        raise RuntimeError('登录已退出，请重新登录。')
                    self._repository.save(provider.id, refreshed)
                    value = refreshed
            headers = {key: val for key, val in provider.get_headers(model_id).items()
                       if key.lower() not in {'authorization', 'chatgpt-account-id', 'host', 'x-api-key'}}
            headers.update({'Authorization': 'Bearer ' + value['access_token'], 'ChatGPT-Account-Id': value['account_id'],
                            'OpenAI-Beta': 'responses=experimental', 'originator': 'pycat'})
            return headers
