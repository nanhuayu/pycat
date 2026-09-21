"""Domestic WorkBuddy browser authentication, owned by the PyCat application.

Protocol reference: CodeBuddy CLI 2.155.0. No CLI process, external credential
import, account pool, or Agent execution is involved.
"""
from __future__ import annotations

import asyncio
import math
import threading
import time
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from pycat.core.app.repositories.provider_credentials import ProviderCredentialsRepository
from pycat.models.provider import WORKBUDDY_ORIGIN

# /v3/config parses this protocol version from User-Agent (business code 12403
# when absent). Identity follows the CLI product/client-info contract, not the
# WorkBuddy desktop host's CLIENT_INFO_* overrides.
WORKBUDDY_CLIENT_VERSION = '2.155.0'
_CLIENT_HEADERS = {
    'User-Agent': f'CLI/{WORKBUDDY_CLIENT_VERSION} CodeBuddy/{WORKBUDDY_CLIENT_VERSION}',
    'X-IDE-Type': 'CLI',
    'X-IDE-Name': 'CLI',
    'X-IDE-Version': WORKBUDDY_CLIENT_VERSION,
    'X-Product': 'SaaS',
}
_AUTH_DOMAINS = {'www.workbuddy.cn', 'www.codebuddy.cn', 'copilot.tencent.com', 'wb.tencentbuddy.com'}
_RESERVED_HEADERS = {'authorization', 'x-api-key', 'host', 'cookie', 'x-refresh-token', 'user-agent',
                     'x-user-id', 'x-enterprise-id', 'x-tenant-id', 'x-domain', 'x-auth-refresh-source',
                     *(key.lower() for key in _CLIENT_HEADERS)}


class WorkBuddyLogin:
    def __init__(self, provider_id):
        self.provider_id = provider_id
        self.cancelled = threading.Event()
        self.authorization_url = ''
        self.state = ''
        self.created_at = time.monotonic()
        self.client = None

    def cancel(self):
        self.cancelled.set()
        if self.client is not None:
            self.client.close()


class WorkBuddyAuthService:
    def __init__(self, repository: ProviderCredentialsRepository, *, transport_factory=None):
        self._repository = repository
        self._transport_factory = transport_factory
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._flows = {}
        self._generation = {}
        self._closed = False

    def _client(self):
        return httpx.Client(
            base_url=WORKBUDDY_ORIGIN, timeout=12, follow_redirects=False,
            headers={'Accept': 'application/json', **_CLIENT_HEADERS},
            transport=self._transport_factory() if self._transport_factory else None,
        )

    @staticmethod
    def _request(client, method, path, stage, *, pending=(), **kwargs):
        try:
            response = client.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise RuntimeError(f'WorkBuddy {stage}无法连接服务，请检查网络后重试。') from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        code = body.get('code') if isinstance(body, dict) else None
        if response.status_code == 200 and str(code) in {str(item) for item in pending}:
            return None
        if response.status_code != 200 or str(code) != '0':
            safe_code = str(code) if str(code).isdigit() else '未知'
            error = RuntimeError(f'WorkBuddy {stage}失败（HTTP {response.status_code}，业务码 {safe_code}）。')
            error.invalid_credentials = response.status_code in {401, 403}
            raise error
        return body.get('data')

    def begin_login(self, provider_id):
        # Lightweight, safe on the GUI thread. Network preparation is separate.
        with self._lock:
            if self._closed:
                raise RuntimeError('登录服务已关闭。')
            previous = self._flows.pop(provider_id, None)
            if previous:
                previous.cancel()
            self._generation[provider_id] = self._generation.get(provider_id, 0) + 1
            flow = WorkBuddyLogin(provider_id)
            self._flows[provider_id] = flow
            return flow

    def pending_login(self, provider_id):
        with self._lock:
            return self._flows.get(provider_id)

    def _check_flow(self, flow, *, timeout=300):
        with self._lock:
            if self._closed or flow.cancelled.is_set() or self._flows.get(flow.provider_id) is not flow:
                raise RuntimeError('登录已取消。')
        if time.monotonic() - flow.created_at >= timeout:
            raise RuntimeError('WorkBuddy 登录等待超时，请重试。')

    def _close_flow(self, flow):
        if flow.client is not None:
            flow.client.close()
        with self._lock:
            if self._flows.get(flow.provider_id) is flow:
                self._flows.pop(flow.provider_id)

    def prepare_login(self, flow):
        try:
            self._check_flow(flow)
            flow.client = self._client()
            data = self._request(flow.client, 'POST', '/v2/plugin/auth/state', '申请授权',
                                 params={'platform': 'CLI'}, json={})
            data = data if isinstance(data, dict) else {}
            state, address = str(data.get('state') or ''), str(data.get('authUrl') or '')
            try:
                url = urlsplit(address)
                valid = (url.scheme == 'https' and url.hostname in _AUTH_DOMAINS
                         and url.path == '/login' and url.port in {None, 443}
                         and not url.username and not url.password and not url.fragment
                         and state and len(state) < 4096 and len(address) < 16384
                         and parse_qs(url.query).get('state') == [state])
            except ValueError:
                valid = False
            if not valid:
                raise RuntimeError('WorkBuddy 返回了无效的授权地址。')
            self._check_flow(flow)
            flow.state, flow.authorization_url = state, address
            return flow
        except Exception:
            self._close_flow(flow)
            raise

    @staticmethod
    def _tokens(data, previous=None):
        previous = previous or {}
        if not isinstance(data, dict):
            raise RuntimeError('WorkBuddy 返回了不完整的凭据。')
        access = str(data.get('accessToken') or '')
        refresh = str(data.get('refreshToken') or previous.get('refresh_token') or '')
        domain = str(data.get('domain') or previous.get('domain') or 'www.workbuddy.cn')
        try:
            expires = float(data.get('expiresAt') or 0)
            if expires >= 100_000_000_000:
                expires /= 1000
            if not expires:
                expires = time.time() + float(data.get('expiresIn') or 0)
            valid = math.isfinite(expires) and expires > time.time()
        except (ValueError, TypeError, OverflowError):
            valid = False
        if domain not in _AUTH_DOMAINS:
            raise RuntimeError('此入口仅支持 WorkBuddy / CodeBuddy 国内账号。')
        if not valid or not access or not refresh or any(c in access + refresh for c in '\r\n'):
            raise RuntimeError('WorkBuddy 返回了不完整或过期的凭据，请重新登录。')
        return {'access_token': access, 'refresh_token': refresh, 'expires_at': expires, 'domain': domain}

    @staticmethod
    def _account(data):
        if not isinstance(data, dict) or not data.get('uid'):
            raise RuntimeError('WorkBuddy 未返回有效账号，请重新授权。')
        result = {key: str(data.get(key) or '') for key in ('uid', 'enterpriseId', 'type', 'nickname')}
        if any(c in ''.join(result.values()) for c in '\r\n'):
            raise RuntimeError('WorkBuddy 账号信息格式异常。')
        return result

    @staticmethod
    def _identity(account):
        return tuple(str(account.get(key) or '') for key in ('uid', 'enterpriseId', 'type'))

    @staticmethod
    def _auth_headers(value):
        headers = {'Authorization': 'Bearer ' + value['access_token'], 'X-Domain': value['domain']}
        account = value.get('account', {})
        if account.get('uid'):
            headers['X-User-Id'] = quote(account['uid'], safe='')
        if account.get('enterpriseId'):
            headers['X-Enterprise-Id'] = account['enterpriseId']
            headers['X-Tenant-Id'] = account['enterpriseId']
        return headers

    def finish_login(self, flow, *, timeout=300, poll_interval=3):
        value = account = None
        try:
            while True:
                self._check_flow(flow, timeout=timeout)
                if flow.client is None or not flow.state:
                    raise RuntimeError('请先准备 WorkBuddy 授权地址。')
                if value is None:
                    data = self._request(flow.client, 'GET', '/v2/plugin/auth/token', '等待授权',
                                         params={'state': flow.state}, pending=(11217,))
                    if data is not None:
                        value = self._tokens(data)
                self._check_flow(flow, timeout=timeout)
                if value is not None and account is None:
                    data = self._request(flow.client, 'GET', '/v2/plugin/login/account', '读取账号',
                                         params={'state': flow.state}, headers=self._auth_headers(value), pending=(12151,))
                    if data is not None:
                        account = self._account(data)
                if account is not None:
                    value['account'] = account
                    with self._lock:
                        self._check_flow(flow, timeout=timeout)
                        self._repository.save('workbuddy:' + flow.provider_id, value)
                        return self.status(flow.provider_id)
                flow.cancelled.wait(poll_interval)
        finally:
            self._close_flow(flow)

    def status(self, provider_id):
        with self._lock:
            value = self._repository.load('workbuddy:' + provider_id)
        account = (value or {}).get('account', {})
        label = account.get('nickname') or account.get('uid', '')
        if account.get('enterpriseId'):
            label += ' · ' + ('个人账号' if account.get('type') == 'personal' else account['enterpriseId'])
        return {'connected': bool(value), 'label': label}

    def logout(self, provider_id):
        with self._lock:
            self._generation[provider_id] = self._generation.get(provider_id, 0) + 1
            flow = self._flows.pop(provider_id, None)
            if flow:
                flow.cancel()
            self._repository.delete('workbuddy:' + provider_id)

    def close(self):
        with self._lock:
            self._closed = True
            for flow in self._flows.values():
                flow.cancel()
            self._flows.clear()

    def _credentials(self, provider_id):
        with self._refresh_lock:
            with self._lock:
                generation = self._generation.get(provider_id, 0)
                value = self._repository.load('workbuddy:' + provider_id)
                if self._closed or not value:
                    raise RuntimeError('请先在模型与服务中登录 WorkBuddy。')
            if value['expires_at'] <= time.time() + 120:
                try:
                    with self._client() as client:
                        headers = {**self._auth_headers(value), 'X-Refresh-Token': value['refresh_token'], 'X-Auth-Refresh-Source': 'plugin'}
                        data = self._request(client, 'POST', '/v2/plugin/auth/token/refresh', '刷新登录', headers=headers, json={})
                    value = {**self._tokens(data, value), 'account': value['account']}
                except RuntimeError as exc:
                    if getattr(exc, 'invalid_credentials', False):
                        with self._lock:
                            if generation == self._generation.get(provider_id, 0):
                                self._repository.delete('workbuddy:' + provider_id)
                    raise
                with self._lock:
                    if self._closed or generation != self._generation.get(provider_id, 0):
                        raise RuntimeError('登录已退出或更改，请重新登录。')
                    self._repository.save('workbuddy:' + provider_id, value)
            return value

    async def headers(self, provider, model_id=''):
        value = await asyncio.to_thread(self._credentials, provider.id)
        headers = {key: val for key, val in provider.get_headers(model_id).items() if key.lower() not in _RESERVED_HEADERS}
        headers.update(self._auth_headers(value))
        headers.update(_CLIENT_HEADERS)
        return headers

    def _account_request(self, provider_id, *, models):
        value = self._credentials(provider_id)
        account = value['account']
        if models:
            path = ('/console/enterprises/' + quote(account['enterpriseId'], safe='') + '/config/models'
                    if account.get('enterpriseId') else '/v3/config')
        else:
            path = '/v2/plugin/accounts'
        with self._client() as client:
            data = self._request(client, 'GET', path, '同步模型' if models else '检查账号', headers=self._auth_headers(value))
        if models and not account.get('enterpriseId'):
            # Personal identities have no enterprise scope. The official CLI
            # gets their live model declarations from cloud product config.
            # Import only model metadata, never prompts, endpoints or tools.
            return data.get('models') if isinstance(data, dict) else None
        if not models:
            accounts = data.get('accounts') if isinstance(data, dict) else None
            if not isinstance(accounts, list):
                raise RuntimeError('WorkBuddy 账号列表格式异常，请稍后重试。')
            if not any(isinstance(item, dict) and item.get('uid')
                       and self._identity(item) == self._identity(account) for item in accounts):
                raise RuntimeError('WorkBuddy 当前身份已不可用，请退出后重新登录。')
        return data

    async def model_catalog(self, provider):
        return await asyncio.to_thread(self._account_request, provider.id, models=True)

    async def check_connection(self, provider):
        await asyncio.to_thread(self._account_request, provider.id, models=False)
