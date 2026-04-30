from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from core.channel.models import _WeChatServerHandle
from core.channel.sources.context import ChannelRuntimeContext
from core.channel.sources.wechat.protocol import (
    build_wechat_text_reply,
    parse_wechat_message,
    verify_wechat_signature,
)
from core.config.schema import ChannelConfig


logger = logging.getLogger(__name__)


def start_wechat_webhook_server(context: ChannelRuntimeContext, channel: ChannelConfig) -> _WeChatServerHandle | None:
    config = dict(getattr(channel, "config", {}) or {})
    host = str(config.get("listen_host", "127.0.0.1") or "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(str(config.get("listen_port", "18963") or "18963").strip())
    except Exception:
        port = 18963
    path = str(config.get("callback_path", f"/wechat/{channel.id}") or f"/wechat/{channel.id}").strip() or f"/wechat/{channel.id}"

    class _WeChatServer(ThreadingHTTPServer):
        daemon_threads = True

        def __init__(self, server_address):
            super().__init__(server_address, _WeChatHandler)
            self.context = context
            self.channel = channel
            self.callback_path = path

    class _WeChatHandler(BaseHTTPRequestHandler):
        server_version = "PyCatWeChat/1.0"

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            logger.debug("WeChat callback %s - %s", self.address_string(), format % args)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != self.server.callback_path:
                self._write_response(404, b"not found")
                return
            params = parse_qs(parsed.query)
            signature = params.get("signature", [""])[0]
            timestamp = params.get("timestamp", [""])[0]
            nonce = params.get("nonce", [""])[0]
            echostr = params.get("echostr", [""])[0]
            token = str((self.server.channel.config or {}).get("token", "") or "").strip()
            if not verify_wechat_signature(token, signature, timestamp, nonce):
                self._write_response(403, b"invalid signature")
                return
            self._write_response(200, str(echostr or "").encode("utf-8"))

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != self.server.callback_path:
                self._write_response(404, b"not found")
                return
            params = parse_qs(parsed.query)
            signature = params.get("signature", [""])[0]
            timestamp = params.get("timestamp", [""])[0]
            nonce = params.get("nonce", [""])[0]
            token = str((self.server.channel.config or {}).get("token", "") or "").strip()
            if not verify_wechat_signature(token, signature, timestamp, nonce):
                self._write_response(403, b"invalid signature")
                return

            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                inbound = parse_wechat_message(raw.decode("utf-8", errors="ignore"))
            except Exception as exc:
                logger.warning("Failed to parse WeChat callback payload: %s", exc)
                self._write_response(400, b"invalid payload")
                return

            if inbound.encrypted:
                logger.warning(
                    "WeChat channel %s received encrypted payload, but AES decryption is not implemented yet.",
                    getattr(self.server.channel, "id", ""),
                )
                self._write_response(200, b"success")
                return

            if inbound.is_text:
                if self.server.context.mark_recent_message(self.server.channel.id, inbound.dedupe_key):
                    self.server.context.process_wechat_delivery(self.server.channel, inbound)
                reply_xml = build_wechat_text_reply(
                    to_user=inbound.from_user,
                    from_user=inbound.to_user,
                    content="已收到消息，正在处理中…",
                )
                self._write_xml(reply_xml)
                return

            if inbound.msg_type == "event" and inbound.event == "subscribe":
                reply_xml = build_wechat_text_reply(
                    to_user=inbound.from_user,
                    from_user=inbound.to_user,
                    content="已连接到 PyCat，后续可直接发送文本消息。",
                )
                self._write_xml(reply_xml)
                return

            self._write_response(200, b"success")

        def _write_xml(self, body: str) -> None:
            payload = str(body or "").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _write_response(self, status: int, body: bytes) -> None:
            payload = body if isinstance(body, bytes) else bytes(body or b"")
            self.send_response(int(status))
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    try:
        httpd = _WeChatServer((host, port))
    except OSError as exc:
        logger.warning(
            "Failed to start WeChat channel server %s on %s:%s%s: %s",
            getattr(channel, "id", ""),
            host,
            port,
            path,
            exc,
        )
        return None

    thread = threading.Thread(
        target=httpd.serve_forever,
        name=f"PyCat-WeChat-{channel.id}",
        daemon=True,
    )
    thread.start()
    logger.info(
        "Started WeChat channel server %s at http://%s:%s%s",
        getattr(channel, "id", ""),
        host,
        port,
        path,
    )
    return _WeChatServerHandle(channel_id=channel.id, httpd=httpd, thread=thread)


__all__ = ["start_wechat_webhook_server"]