from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from core.channel.models import ChannelServerHandle
from core.channel.sources.context import ChannelRuntimeContext
from core.channel.sources.feishu.router import normalize_feishu_webhook_payload
from core.config.schema import ChannelConfig


logger = logging.getLogger(__name__)


def start_feishu_webhook_server(context: ChannelRuntimeContext, channel: ChannelConfig) -> ChannelServerHandle | None:
    config = dict(getattr(channel, "config", {}) or {})
    host = str(config.get("listen_host", "127.0.0.1") or "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(str(config.get("listen_port", "18964") or "18964").strip())
    except Exception:
        port = 18964
    path = str(config.get("callback_path", f"/feishu/{channel.id}") or f"/feishu/{channel.id}").strip() or f"/feishu/{channel.id}"

    class _FeishuServer(ThreadingHTTPServer):
        daemon_threads = True

        def __init__(self, server_address):
            super().__init__(server_address, _FeishuHandler)
            self.context = context
            self.channel = channel
            self.callback_path = path

    class _FeishuHandler(BaseHTTPRequestHandler):
        server_version = "PyCatFeishu/1.0"

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            logger.debug("Feishu callback %s - %s", self.address_string(), format % args)

        def do_GET(self) -> None:  # noqa: N802
            self._write_json(405, {"code": 405, "msg": "method not allowed"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != self.server.callback_path:
                self._write_json(404, {"code": 404, "msg": "not found"})
                return

            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                payload = json.loads(raw.decode("utf-8", errors="ignore") or "{}")
            except Exception as exc:
                logger.warning("Failed to parse Feishu callback payload: %s", exc)
                self._write_json(400, {"code": 400, "msg": "invalid payload"})
                return

            token = str((self.server.channel.config or {}).get("verification_token", "") or "").strip()
            try:
                envelope = normalize_feishu_webhook_payload(
                    payload,
                    expected_token=token,
                    mark_recent=lambda message_id: self.server.context.mark_recent_message(self.server.channel.id, message_id),
                )
            except ValueError as exc:
                detail = str(exc or "invalid payload")
                logger.warning("Rejected Feishu callback for channel %s: %s", getattr(self.server.channel, "id", ""), detail)
                status = 403 if "token" in detail else 400
                self._write_json(status, {"code": status, "msg": detail})
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

        def _write_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(int(status))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    try:
        httpd = _FeishuServer((host, port))
    except OSError as exc:
        logger.warning(
            "Failed to start Feishu channel server %s on %s:%s%s: %s",
            getattr(channel, "id", ""),
            host,
            port,
            path,
            exc,
        )
        return None

    thread = threading.Thread(
        target=httpd.serve_forever,
        name=f"PyCat-Feishu-{channel.id}",
        daemon=True,
    )
    thread.start()
    logger.info(
        "Started Feishu channel server %s at http://%s:%s%s",
        getattr(channel, "id", ""),
        host,
        port,
        path,
    )
    return ChannelServerHandle(channel_id=channel.id, httpd=httpd, thread=thread)


__all__ = ["start_feishu_webhook_server"]