from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import StrEnum
from http.server import ThreadingHTTPServer
from typing import Any, Protocol


class ChannelConnectionHandle(Protocol):
    channel_id: str

    def stop(self) -> None:
        ...


class ChannelConnectionState(StrEnum):
    DISABLED = "disabled"
    INCOMPLETE = "incomplete"
    CONNECTING = "connecting"
    WAITING_USER = "waiting_user"
    READY = "ready"
    RECONNECTING = "reconnecting"
    ERROR = "error"


class ChannelRequiredAction(StrEnum):
    NONE = "none"
    SCAN = "scan"
    VERIFY_CODE = "verify_code"
    RETRY = "retry"


@dataclass
class ChannelServerHandle:
    channel_id: str
    httpd: ThreadingHTTPServer
    thread: threading.Thread

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.thread.is_alive():
            self.thread.join(timeout=2.0)


@dataclass(frozen=True)
class ChannelConnectionSnapshot:
    channel_id: str
    channel_type: str
    mode: str = ""
    state: ChannelConnectionState = ChannelConnectionState.DISABLED
    required_action: ChannelRequiredAction = ChannelRequiredAction.NONE
    detail: str = ""
    qr_text: str = ""
    expires_at: str = ""
    account_name: str = ""
    session_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", _connection_state(self.state))
        object.__setattr__(self, "required_action", _required_action(self.required_action))

    @property
    def is_ready(self) -> bool:
        return self.state == ChannelConnectionState.READY

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "channel_type": self.channel_type,
            "mode": self.mode,
            "state": self.state.value,
            "required_action": self.required_action.value,
            "detail": self.detail,
            "qr_text": self.qr_text,
            "expires_at": self.expires_at,
            "account_name": self.account_name,
            "session_id": self.session_id,
            "raw": dict(self.raw or {}),
        }


def _connection_state(value: ChannelConnectionState | str) -> ChannelConnectionState:
    try:
        return ChannelConnectionState(str(value or "").strip().lower())
    except ValueError:
        return ChannelConnectionState.ERROR


def _required_action(value: ChannelRequiredAction | str) -> ChannelRequiredAction:
    try:
        return ChannelRequiredAction(str(value or "").strip().lower())
    except ValueError:
        return ChannelRequiredAction.NONE


__all__ = [
    "ChannelConnectionHandle",
    "ChannelConnectionSnapshot",
    "ChannelConnectionState",
    "ChannelRequiredAction",
    "ChannelServerHandle",
]
