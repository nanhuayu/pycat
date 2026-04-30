from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal

from core.channel.runtime import ChannelRuntimeEvent, ChannelRuntimeService


class ChannelRuntimeBridge(QObject):
    """Qt bridge that forwards background channel runtime events onto the UI thread."""

    conversation_updated = pyqtSignal(object)
    token_received = pyqtSignal(str, str, str)
    thinking_received = pyqtSignal(str, str, str)
    response_step = pyqtSignal(str, str, object)
    response_complete = pyqtSignal(str, str, object)
    response_error = pyqtSignal(str, str, str)
    runtime_event = pyqtSignal(str, str, object)

    def __init__(self, runtime: ChannelRuntimeService, parent=None) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._runtime.add_event_listener(self._handle_runtime_event)

    def dispose(self) -> None:
        try:
            self._runtime.remove_event_listener(self._handle_runtime_event)
        except Exception:
            return

    def _handle_runtime_event(self, event: ChannelRuntimeEvent) -> None:
        kind = str(getattr(event, "kind", "") or "").strip()
        conversation_id = str(getattr(event, "conversation_id", "") or "").strip()
        request_id = str(getattr(event, "request_id", "") or "").strip()
        payload = getattr(event, "payload", {}) or {}
        if kind == "turn-token":
            self.token_received.emit(conversation_id, request_id, str(payload.get("token", "") or ""))
            return
        if kind == "turn-thinking":
            self.thinking_received.emit(conversation_id, request_id, str(payload.get("thinking", "") or ""))
            return
        if kind == "turn-step":
            message = payload.get("message") if isinstance(payload, dict) else None
            self.response_step.emit(conversation_id, request_id, message)
            return
        if kind == "turn-complete":
            message = payload.get("message") if isinstance(payload, dict) else None
            self.response_complete.emit(conversation_id, request_id, message)
            return
        if kind == "turn-error":
            self.response_error.emit(conversation_id, request_id, str(payload.get("error", "") or ""))
            return
        if kind == "turn-event":
            self.runtime_event.emit(conversation_id, request_id, payload.get("event") if isinstance(payload, dict) else event)
            return
        self.conversation_updated.emit(event)
