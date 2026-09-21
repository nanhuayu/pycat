"""Official Stream wire adapter using the existing connection lifecycle."""
import asyncio
import json
from urllib.parse import urlencode, urlsplit

import httpx
import websockets

from pycat.core.channel.async_connection import AsyncConnectionHandle, run_async_connection_thread
from pycat.core.channel.connection import ChannelConnectionSnapshot, ChannelConnectionState
from pycat.core.channel.platforms.dingtalk.client import DINGTALK_API
from pycat.core.channel.platforms.dingtalk.router import normalize_dingtalk_message


BOT_TOPIC = '/v1.0/im/bot/messages/get'


class DingTalkStreamRunner:
    def __init__(self, context, channel, handle):
        self.context, self.channel, self.handle = context, channel, handle

    def stopped(self):
        return self.handle.stop_event.is_set() or self.context.is_stopping()

    async def run(self):
        if self.stopped():
            return
        connection = asyncio.create_task(self._connect_loop())
        stop = asyncio.create_task(self._wait_stop())
        try:
            await asyncio.wait({connection, stop}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            connection.cancel()
            stop.cancel()
            await asyncio.gather(connection, stop, return_exceptions=True)
            self.handle.websocket = None

    async def _wait_stop(self):
        while not self.stopped():
            await asyncio.sleep(0.2)

    def report(self, state, detail):
        if not self.stopped():
            self.context.report_connection(ChannelConnectionSnapshot(self.channel.id, 'dingtalk',
                mode='stream', state=state, detail=detail))

    async def _connect_loop(self):
        delay = 1
        while not self.stopped():
            try:
                self.report(ChannelConnectionState.CONNECTING, '正在连接钉钉 Stream。')
                url = await self.connection_url()
                async with websockets.connect(url, open_timeout=15, close_timeout=3,
                        ping_interval=30, ping_timeout=20, max_size=2 * 1024 * 1024) as socket:
                    self.handle.websocket = socket
                    self.report(ChannelConnectionState.READY, '钉钉 Stream 已连接，无需公网回调。')
                    delay = 1
                    async for raw in socket:
                        try:
                            ack, disconnect = self.dispatch(json.loads(raw))
                        except (ValueError, TypeError, KeyError):
                            continue
                        await socket.send(json.dumps(ack, ensure_ascii=False))
                        if disconnect or self.stopped():
                            break
            except (httpx.HTTPError, OSError, ValueError, websockets.exceptions.WebSocketException):
                self.report(ChannelConnectionState.ERROR, '钉钉连接失败，将自动重试；请检查网络、Client ID / Secret 和 Stream 配置。')
            finally:
                self.handle.websocket = None
            if not self.stopped():
                self.report(ChannelConnectionState.RECONNECTING, '钉钉连接已断开，正在重连。')
                await asyncio.sleep(delay)
                delay = min(30, delay * 2)

    async def connection_url(self):
        config = self.channel.config
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(DINGTALK_API + '/v1.0/gateway/connections/open', json={
                'clientId': config.get('client_id'), 'clientSecret': config.get('client_secret'),
                'subscriptions': [{'type': 'CALLBACK', 'topic': BOT_TOPIC}], 'ua': 'PyCat/Stream'})
            response.raise_for_status()
            data = response.json()
        endpoint, ticket = str(data.get('endpoint') or ''), str(data.get('ticket') or '')
        parsed = urlsplit(endpoint)
        host = parsed.hostname or ''
        if (parsed.scheme != 'wss' or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.port not in (None, 443) or not (host == 'dingtalk.com' or host.endswith('.dingtalk.com')) or not ticket):
            raise ValueError('钉钉返回了无效连接地址。')
        return endpoint + '?' + urlencode({'ticket': ticket})

    def dispatch(self, frame):
        if not isinstance(frame, dict) or not isinstance(frame.get('headers'), dict):
            raise ValueError('钉钉消息帧格式无效。')
        headers = frame['headers']
        data = frame.get('data') or '{}'
        data = json.loads(data) if isinstance(data, str) else data
        kind, topic = frame.get('type'), headers.get('topic')
        code, reply, disconnect = 200, {'response': 'OK'}, False
        if kind == 'SYSTEM':
            reply = data
            disconnect = topic == 'disconnect'
        elif kind == 'CALLBACK' and topic == BOT_TOPIC:
            envelope = normalize_dingtalk_message(data,
                mark_recent=lambda key: self.context.mark_recent_message(self.channel.id, key))
            if envelope:
                self.context.enqueue_channel_message(self.channel, envelope.content, meta=envelope.meta,
                    **({'media': envelope.media} if envelope.media else {}))
        else:
            code = 404
        return {'code': code, 'headers': {'messageId': headers.get('messageId', ''),
            'contentType': 'application/json'}, 'message': 'OK' if code == 200 else 'Unsupported topic',
            'data': json.dumps(reply, ensure_ascii=False)}, disconnect


def start_dingtalk_stream(context, channel):
    handle = AsyncConnectionHandle(channel.id)
    runner = DingTalkStreamRunner(context, channel, handle)
    return run_async_connection_thread(handle, thread_name=f'dingtalk-{channel.id}', run=runner.run)
