from __future__ import annotations

import asyncio
import http
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import websockets

from core.channel.sources.context import ChannelRuntimeContext
from core.channel.sources.feishu.router import normalize_feishu_webhook_payload
from core.channel.sources.feishu.ws_protocol import (
    GEN_ENDPOINT_URI,
    HEADER_BIZ_RT,
    HEADER_MESSAGE_ID,
    HEADER_SEQ,
    HEADER_SUM,
    HEADER_TYPE,
    OK,
    SERVICE_ID,
    UTF_8,
    FeishuFrame,
    FrameType,
    MessageType,
    build_ping_frame,
    build_response_frame,
    decode_frame,
    encode_frame,
)
from core.config.schema import ChannelConfig


logger = logging.getLogger(__name__)


@dataclass
class FeishuWebSocketClientHandle:
    channel_id: str
    stop_event: threading.Event
    thread: threading.Thread | None = None
    loop: asyncio.AbstractEventLoop | None = None
    websocket: Any = None
    partial_payloads: dict[str, list[bytes]] = field(default_factory=dict)


class _FeishuWebSocketRunner:
    def __init__(self, context: ChannelRuntimeContext, channel: ChannelConfig, handle: FeishuWebSocketClientHandle) -> None:
        self._context = context
        self._channel = channel
        self._handle = handle
        self._reconnect_interval = 8
        self._ping_interval = 120
        self._service_id = 0

    async def run(self) -> None:
        while not self._handle.stop_event.is_set() and not self._context.is_stopping():
            try:
                conn_url = await self._fetch_conn_url()
                parsed = urlparse(conn_url)
                params = parse_qs(parsed.query)
                try:
                    self._service_id = int((params.get(SERVICE_ID) or ["0"])[0] or "0")
                except Exception:
                    self._service_id = 0

                async with websockets.connect(conn_url) as websocket:
                    self._handle.websocket = websocket
                    self._report_status(
                        status="ready",
                        detail="飞书长连接已建立，无需公网回调。",
                    )
                    ping_task = asyncio.create_task(self._ping_loop())
                    stop_task = asyncio.create_task(self._wait_for_stop())
                    receive_task = asyncio.create_task(self._receive_loop(websocket))
                    done, pending = await asyncio.wait(
                        {ping_task, stop_task, receive_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    for task in pending:
                        task.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)

                    for task in done:
                        if task is stop_task:
                            continue
                        exc = task.exception()
                        if exc is not None and not self._handle.stop_event.is_set():
                            raise exc

                    if self._handle.stop_event.is_set() or self._context.is_stopping():
                        break
            except Exception as exc:
                if self._handle.stop_event.is_set() or self._context.is_stopping():
                    break
                logger.warning(
                    "Feishu websocket worker failed for channel %s: %s",
                    self._handle.channel_id,
                    exc,
                )
                self._report_status(
                    status="error",
                    detail=f"飞书长连接异常：{exc}",
                )
                await asyncio.sleep(self._reconnect_interval)
            finally:
                self._handle.websocket = None

    async def _wait_for_stop(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._handle.stop_event.wait)

    async def _fetch_conn_url(self) -> str:
        config = dict(getattr(self._channel, "config", {}) or {})
        app_id = str(config.get("app_id", "") or "").strip()
        app_secret = str(config.get("app_secret", "") or "").strip()
        if not (app_id and app_secret):
            raise RuntimeError("缺少 App ID / App Secret，无法建立飞书长连接。")

        base_url = str(config.get("open_base_url", "https://open.feishu.cn") or "https://open.feishu.cn").strip().rstrip("/")
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                f"{base_url}{GEN_ENDPOINT_URI}",
                headers={"locale": "zh"},
                json={
                    "AppID": app_id,
                    "AppSecret": app_secret,
                },
            )
            response.raise_for_status()
            payload = response.json()

        code = int(payload.get("code", 0) or 0)
        if code != OK:
            raise RuntimeError(payload.get("msg") or payload.get("message") or "获取飞书长连接地址失败")

        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        client_config = data.get("ClientConfig") if isinstance(data.get("ClientConfig"), dict) else {}
        self._reconnect_interval = max(2, int(client_config.get("ReconnectInterval", 8) or 8))
        self._ping_interval = max(20, int(client_config.get("PingInterval", 120) or 120))
        conn_url = str(data.get("URL", "") or "").strip()
        if not conn_url:
            raise RuntimeError("飞书长连接地址为空")
        return conn_url

    async def _ping_loop(self) -> None:
        while not self._handle.stop_event.is_set() and not self._context.is_stopping():
            await asyncio.sleep(self._ping_interval)
            websocket = self._handle.websocket
            if websocket is None or self._service_id <= 0:
                continue
            try:
                await websocket.send(encode_frame(build_ping_frame(self._service_id)))
            except Exception as exc:
                if self._handle.stop_event.is_set() or self._context.is_stopping():
                    return
                raise RuntimeError(f"发送飞书长连接 ping 失败：{exc}") from exc

    async def _receive_loop(self, websocket: Any) -> None:
        while not self._handle.stop_event.is_set() and not self._context.is_stopping():
            message = await websocket.recv()
            await self._handle_message(websocket, message)

    async def _handle_message(self, websocket: Any, raw_message: Any) -> None:
        payload_bytes = raw_message.encode(UTF_8) if isinstance(raw_message, str) else bytes(raw_message)
        frame = decode_frame(payload_bytes)
        frame_type = FrameType(frame.method)
        if frame_type == FrameType.CONTROL:
            self._handle_control_frame(frame)
            return
        if frame_type != FrameType.DATA:
            return

        payload = self._reassemble_payload(frame)
        if payload is None:
            return

        response_code = http.HTTPStatus.OK
        start_ms = int(round(time.time() * 1000))
        try:
            self._dispatch_event_payload(payload)
        except Exception as exc:
            logger.warning(
                "Failed to process Feishu websocket payload for channel %s: %s",
                self._handle.channel_id,
                exc,
            )
            response_code = http.HTTPStatus.INTERNAL_SERVER_ERROR
        finally:
            elapsed = int(round(time.time() * 1000)) - start_ms
            response_frame = build_response_frame(frame, code=int(response_code), biz_rt_ms=elapsed)
            await websocket.send(encode_frame(response_frame))

    def _handle_control_frame(self, frame: FeishuFrame) -> None:
        type_value = self._get_header(frame, HEADER_TYPE)
        if type_value != MessageType.PONG.value:
            return
        raw = bytes(frame.payload or b"")
        if not raw:
            return
        try:
            payload = json.loads(raw.decode(UTF_8, errors="ignore") or "{}")
        except Exception:
            return
        self._reconnect_interval = max(2, int(payload.get("ReconnectInterval", self._reconnect_interval) or self._reconnect_interval))
        self._ping_interval = max(20, int(payload.get("PingInterval", self._ping_interval) or self._ping_interval))

    def _reassemble_payload(self, frame: FeishuFrame) -> bytes | None:
        message_type = self._get_header(frame, HEADER_TYPE)
        if message_type != MessageType.EVENT.value:
            return None

        message_id = self._get_header(frame, HEADER_MESSAGE_ID)
        try:
            part_count = int(self._get_header(frame, HEADER_SUM) or "1")
            sequence = int(self._get_header(frame, HEADER_SEQ) or "0")
        except Exception:
            part_count = 1
            sequence = 0

        payload = bytes(frame.payload or b"")
        if part_count <= 1:
            return payload

        bucket = self._handle.partial_payloads.get(message_id)
        if bucket is None or len(bucket) != part_count:
            bucket = [b""] * part_count
            self._handle.partial_payloads[message_id] = bucket
        if 0 <= sequence < len(bucket):
            bucket[sequence] = payload
        if any(not part for part in bucket):
            return None
        self._handle.partial_payloads.pop(message_id, None)
        return b"".join(bucket)

    def _dispatch_event_payload(self, payload: bytes) -> None:
        data = json.loads(payload.decode(UTF_8, errors="ignore") or "{}")
        config = dict(getattr(self._channel, "config", {}) or {})
        token = str(config.get("verification_token", "") or "").strip()
        envelope = normalize_feishu_webhook_payload(
            data,
            expected_token=token,
            mark_recent=lambda message_id: self._context.mark_recent_message(self._channel.id, message_id),
        )
        if envelope is None or envelope.kind != "message":
            return
        self._context.enqueue_channel_message(
            self._channel,
            envelope.content,
            meta=envelope.meta,
        )

    @staticmethod
    def _get_header(frame: FeishuFrame, key: str) -> str:
        return frame.header_value(key)

    def _report_status(self, *, status: str, detail: str) -> None:
        try:
            self._channel = self._context.update_channel_runtime_state(
                self._channel,
                config_updates={
                    "status_detail": detail,
                },
                status=status,
            )
            self._context.remember_channel(self._channel)
        except Exception as exc:
            logger.debug("Failed to persist Feishu websocket state for %s: %s", self._handle.channel_id, exc)


def start_feishu_ws_client(context: ChannelRuntimeContext, channel: ChannelConfig) -> FeishuWebSocketClientHandle | None:
    channel_id = str(getattr(channel, "id", "") or "").strip()
    if not channel_id:
        return None

    existing = context.get_feishu_ws_worker(channel_id)
    if existing and existing.thread is not None and existing.thread.is_alive():
        return existing

    config = dict(getattr(channel, "config", {}) or {})
    if not (str(config.get("app_id", "") or "").strip() and str(config.get("app_secret", "") or "").strip()):
        logger.warning("Skipped Feishu websocket worker for channel %s because credentials are incomplete", channel_id)
        return None

    handle = FeishuWebSocketClientHandle(
        channel_id=channel_id,
        stop_event=threading.Event(),
    )

    def _run() -> None:
        loop = asyncio.new_event_loop()
        handle.loop = loop
        asyncio.set_event_loop(loop)
        runner = _FeishuWebSocketRunner(context, channel, handle)
        try:
            loop.run_until_complete(runner.run())
        except Exception as exc:
            if not handle.stop_event.is_set():
                logger.warning("Feishu websocket thread crashed for channel %s: %s", channel_id, exc)
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                try:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                except Exception:
                    pass
            loop.close()
            handle.loop = None
            handle.websocket = None

    thread = threading.Thread(
        target=_run,
        name=f"PyCat-FeishuWS-{channel_id}",
        daemon=True,
    )
    handle.thread = thread
    context.remember_feishu_ws_worker(channel_id, handle)
    thread.start()
    logger.info("Started Feishu websocket worker for channel %s", channel_id)
    return handle


def stop_feishu_ws_client(handle: FeishuWebSocketClientHandle) -> None:
    handle.stop_event.set()
    loop = handle.loop
    websocket = handle.websocket
    if loop is not None and websocket is not None:
        try:
            asyncio.run_coroutine_threadsafe(websocket.close(), loop)
        except Exception:
            pass


__all__ = ["FeishuWebSocketClientHandle", "start_feishu_ws_client", "stop_feishu_ws_client"]