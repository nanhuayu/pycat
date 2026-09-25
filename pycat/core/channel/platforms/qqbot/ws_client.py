from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

import websockets

from pycat.core.channel.async_connection import AsyncConnectionHandle, run_async_connection_thread
from pycat.core.channel.connection import ChannelConnectionSnapshot, ChannelConnectionState
from pycat.core.channel.host import ChannelHost
from pycat.core.channel.platforms.qqbot.client import QQBotChannelClient
from pycat.core.channel.platforms.qqbot.router import normalize_qqbot_webhook_payload
from pycat.models.contracts.channel import ChannelConfig

logger = logging.getLogger(__name__)


QQBOT_INTENT_PUBLIC_GUILD_MESSAGES = 1 << 30
QQBOT_INTENT_DIRECT_MESSAGE = 1 << 12
QQBOT_INTENT_GROUP_AND_C2C = 1 << 25
QQBOT_INTENT_INTERACTION = 1 << 26
QQBOT_DEFAULT_INTENTS = (
    QQBOT_INTENT_PUBLIC_GUILD_MESSAGES
    | QQBOT_INTENT_DIRECT_MESSAGE
    | QQBOT_INTENT_GROUP_AND_C2C
    | QQBOT_INTENT_INTERACTION
)


@dataclass
class QQBotWebSocketClientHandle(AsyncConnectionHandle):
    session_id: str = ""
    last_seq: int | None = None


class _QQBotWebSocketRunner:
    def __init__(
        self,
        context: ChannelHost,
        channel: ChannelConfig,
        handle: QQBotWebSocketClientHandle,
        client: QQBotChannelClient | None = None,
    ) -> None:
        self._context = context
        self._channel = channel
        self._handle = handle
        self._client = client or QQBotChannelClient()
        self._reconnect_interval = 8

    async def run(self) -> None:
        while not self._handle.stop_event.is_set() and not self._context.is_stopping():
            try:
                config = dict(getattr(self._channel, "config", {}) or {})
                access_token = self._client.resolve_access_token(self._channel, config)
                gateway_url = await asyncio.to_thread(self._client.get_gateway_url, self._channel, access_token=access_token)
                self._report_status(status="connecting", detail="正在连接 QQ Bot 官方 Gateway。")
                async with websockets.connect(gateway_url) as websocket:
                    self._handle.websocket = websocket
                    await self._receive_loop(websocket, access_token)
            except Exception as exc:
                if self._handle.stop_event.is_set() or self._context.is_stopping():
                    break
                logger.warning("QQ Bot websocket worker failed for channel %s: %s", self._handle.channel_id, exc)
                self._report_status(status="error", detail=f"QQ Bot 官方长连接异常：{exc}")
                await asyncio.sleep(self._reconnect_interval)
            finally:
                self._handle.websocket = None

    async def _receive_loop(self, websocket: Any, access_token: str) -> None:
        heartbeat_task: asyncio.Task[Any] | None = None
        stop_task = asyncio.create_task(self._wait_for_stop())
        try:
            while not self._handle.stop_event.is_set() and not self._context.is_stopping():
                receive_task = asyncio.create_task(websocket.recv())
                done, pending = await asyncio.wait({receive_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
                if stop_task in done:
                    receive_task.cancel()
                    await asyncio.gather(receive_task, return_exceptions=True)
                    break
                raw = receive_task.result()
                payload = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8", errors="ignore"))
                op = int(payload.get("op", -1))
                sequence = payload.get("s")
                if sequence is not None:
                    try:
                        self._handle.last_seq = int(sequence)
                    except Exception:
                        pass

                if op == 10:
                    interval_ms = int(((payload.get("d") or {}) if isinstance(payload.get("d"), dict) else {}).get("heartbeat_interval", 45000) or 45000)
                    await self._identify(websocket, access_token)
                    if heartbeat_task is not None:
                        heartbeat_task.cancel()
                        await asyncio.gather(heartbeat_task, return_exceptions=True)
                    heartbeat_task = asyncio.create_task(self._heartbeat_loop(websocket, max(1.0, interval_ms / 1000)))
                elif op == 0:
                    self._handle_dispatch(payload)
                elif op == 7:
                    raise RuntimeError("QQ Bot Gateway 要求重新连接。")
                elif op == 9:
                    self._handle.session_id = ""
                    self._handle.last_seq = None
                    raise RuntimeError("QQ Bot Gateway 会话失效，准备重新连接。")
        finally:
            stop_task.cancel()
            if heartbeat_task is not None:
                heartbeat_task.cancel()
            await asyncio.gather(stop_task, *( [heartbeat_task] if heartbeat_task is not None else [] ), return_exceptions=True)

    async def _wait_for_stop(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._handle.stop_event.wait)

    async def _identify(self, websocket: Any, access_token: str) -> None:
        await websocket.send(
            json.dumps(
                {
                    "op": 2,
                    "d": {
                        "token": f"QQBot {access_token}",
                        "intents": self._resolve_intents(),
                        "shard": [0, 1],
                    },
                },
                ensure_ascii=False,
            )
        )

    async def _heartbeat_loop(self, websocket: Any, interval_seconds: float) -> None:
        while not self._handle.stop_event.is_set() and not self._context.is_stopping():
            await asyncio.sleep(interval_seconds)
            await websocket.send(json.dumps({"op": 1, "d": self._handle.last_seq}, ensure_ascii=False))

    def _handle_dispatch(self, payload: dict[str, Any]) -> None:
        event_type = str(payload.get("t", "") or "").strip().upper()
        data = payload.get("d") if isinstance(payload.get("d"), dict) else {}
        if event_type == "READY":
            self._handle.session_id = str(data.get("session_id", "") or "").strip()
            self._report_status(status="ready", detail="QQ Bot 官方长连接已建立，无需公网回调。")
            return
        if event_type == "RESUMED":
            self._report_status(status="ready", detail="QQ Bot 官方长连接已恢复。")
            return
        if event_type not in {
            "C2C_MESSAGE_CREATE",
            "AT_MESSAGE_CREATE",
            "DIRECT_MESSAGE_CREATE",
            "GROUP_AT_MESSAGE_CREATE",
            "GROUP_MESSAGE_CREATE",
        }:
            return

        envelope = normalize_qqbot_webhook_payload(
            payload,
            mark_recent=lambda message_id: self._context.mark_recent_message(self._channel.id, message_id),
        )
        if envelope is None or envelope.kind != "message":
            return
        self._context.enqueue_channel_message(self._channel, envelope.content, meta=envelope.meta,
            **({'media': envelope.media} if envelope.media else {}))

    def _resolve_intents(self) -> int:
        config = dict(getattr(self._channel, "config", {}) or {})
        raw_value = config.get("intents", "")
        try:
            value = int(str(raw_value or "").strip())
        except Exception:
            value = 0
        return value or QQBOT_DEFAULT_INTENTS

    def _report_status(self, *, status: str, detail: str) -> None:
        state = {
            "connecting": ChannelConnectionState.CONNECTING,
            "ready": ChannelConnectionState.READY,
            "error": ChannelConnectionState.RECONNECTING,
        }.get(status, ChannelConnectionState.ERROR)
        self._context.report_connection(
            ChannelConnectionSnapshot(
                channel_id=self._handle.channel_id,
                channel_type="qqbot",
                mode="websocket",
                state=state,
                detail=detail,
            )
        )


def start_qqbot_ws_client(
    context: ChannelHost,
    channel: ChannelConfig,
) -> QQBotWebSocketClientHandle | None:
    channel_id = str(getattr(channel, "id", "") or "").strip()
    if not channel_id:
        return None

    config = dict(getattr(channel, "config", {}) or {})
    app_id = str(config.get("app_id", "") or config.get("appId", "") or config.get("appid", "") or "").strip()
    app_secret = str(config.get("app_secret", "") or config.get("appSecret", "") or config.get("appsecret", "") or config.get("client_secret", "") or config.get("clientSecret", "") or "").strip()
    if not (app_id and app_secret):
        logger.warning("Skipped QQ Bot websocket worker for channel %s because AppID/AppSecret are incomplete", channel_id)
        return None

    handle = QQBotWebSocketClientHandle(channel_id=channel_id)
    runner = _QQBotWebSocketRunner(context, channel, handle)
    run_async_connection_thread(
        handle,
        thread_name=f"PyCat-QQBotWS-{channel_id}",
        run=runner.run,
    )
    logger.info("Started QQ Bot websocket worker for channel %s", channel_id)
    return handle

__all__ = [
    "QQBOT_DEFAULT_INTENTS",
    "QQBOT_INTENT_DIRECT_MESSAGE",
    "QQBOT_INTENT_GROUP_AND_C2C",
    "QQBOT_INTENT_INTERACTION",
    "QQBOT_INTENT_PUBLIC_GUILD_MESSAGES",
    "QQBotWebSocketClientHandle",
    "start_qqbot_ws_client",
]
