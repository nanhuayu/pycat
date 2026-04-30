from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from core.channel.models import ChannelServerHandle
from core.channel.sources.context import ChannelRuntimeContext
from core.channel.sources.qqbot.router import normalize_qqbot_webhook_payload
from core.config.schema import ChannelConfig


logger = logging.getLogger(__name__)


def start_qqbot_webhook_server(context: ChannelRuntimeContext, channel: ChannelConfig) -> ChannelServerHandle | None:
    config = dict(getattr(channel, "config", {}) or {})
    host = str(config.get("listen_host", "127.0.0.1") or "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(str(config.get("listen_port", "18965") or "18965").strip())
    except Exception:
        port = 18965
    path = str(config.get("callback_path", f"/qqbot/{channel.id}") or f"/qqbot/{channel.id}").strip() or f"/qqbot/{channel.id}"

    class _QQBotServer(ThreadingHTTPServer):
        daemon_threads = True

        def __init__(self, server_address):
            super().__init__(server_address, _QQBotHandler)
            self.context = context
            self.channel = channel
            self.callback_path = path

    class _QQBotHandler(BaseHTTPRequestHandler):
        server_version = "PyCatQQBot/1.0"

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            logger.debug("QQBot callback %s - %s", self.address_string(), format % args)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != self.server.callback_path:
                self._write_json(404, {"code": 404, "msg": "not found"})
                return
            self._write_json(200, {"code": 0, "msg": "qqbot webhook ready"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != self.server.callback_path:
                self._write_json(404, {"code": 404, "msg": "not found"})
                return

            if not self._validate_token():
                self._write_json(403, {"code": 403, "msg": "invalid token"})
                return

            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                payload = json.loads(raw.decode("utf-8", errors="ignore") or "{}")
            except Exception as exc:
                logger.warning("Failed to parse QQBot callback payload: %s", exc)
                self._write_json(400, {"code": 400, "msg": "invalid payload"})
                return

            try:
                envelope = normalize_qqbot_webhook_payload(
                    payload,
                    mark_recent=lambda message_id: self.server.context.mark_recent_message(self.server.channel.id, message_id),
                )
            except ValueError as exc:
                detail = str(exc or "invalid payload")
                logger.warning("Rejected QQBot callback for channel %s: %s", getattr(self.server.channel, "id", ""), detail)
                self._write_json(400, {"code": 400, "msg": detail})
                return

            if envelope is None:
                self._write_json(200, {"code": 0})
                return

            if envelope.kind == "challenge":
                self._write_json(200, {"challenge": envelope.challenge})
                return

            self.server.context.enqueue_channel_message(
                self.server.channel,
                envelope.content,
                meta=envelope.meta,
            )
            self._write_json(200, {"code": 0})

        def _validate_token(self) -> bool:
            expected = str((self.server.channel.config or {}).get("webhook_token", "") or "").strip()
            if not expected:
                return True
            candidates = [
                str(self.headers.get("X-PyCat-Token", "") or "").strip(),
                str(self.headers.get("X-QQBot-Token", "") or "").strip(),
            ]
            authorization = str(self.headers.get("Authorization", "") or "").strip()
            if authorization.lower().startswith("bearer "):
                candidates.append(authorization[7:].strip())
            return expected in candidates

        def _write_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(int(status))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    try:
        httpd = _QQBotServer((host, port))
    except OSError as exc:
        logger.warning(
            "Failed to start QQBot channel server %s on %s:%s%s: %s",
            getattr(channel, "id", ""),
            host,
            port,
            path,
            exc,
        )
        return None

    thread = threading.Thread(
        target=httpd.serve_forever,
        name=f"PyCat-QQBot-{channel.id}",
        daemon=True,
    )
    thread.start()
    logger.info("Started QQBot channel server %s at http://%s:%s%s", getattr(channel, "id", ""), host, port, path)
    return ChannelServerHandle(channel_id=channel.id, httpd=httpd, thread=thread)


__all__ = ["start_qqbot_webhook_server"]