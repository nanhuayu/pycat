from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

from core.channel.connection import ChannelServerHandle


def start_http_server(
    *,
    channel_id: str,
    httpd: ThreadingHTTPServer,
    thread_name: str,
) -> ChannelServerHandle:
    thread = threading.Thread(
        target=httpd.serve_forever,
        name=thread_name,
        daemon=True,
    )
    thread.start()
    return ChannelServerHandle(channel_id=str(channel_id or ""), httpd=httpd, thread=thread)


__all__ = ["start_http_server"]
