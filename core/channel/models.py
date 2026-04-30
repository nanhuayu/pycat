from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from core.config.io import get_global_data_dir


logger = logging.getLogger(__name__)


@dataclass
class ChannelServerHandle:
	channel_id: str
	httpd: ThreadingHTTPServer
	thread: threading.Thread


_WeChatServerHandle = ChannelServerHandle


@dataclass
class _WeChatBridgePollerHandle:
	channel_id: str
	stop_event: threading.Event
	uin: str
	thread: threading.Thread | None = None


@dataclass(frozen=True)
class ChannelConnectionSnapshot:
	channel_id: str
	channel_type: str
	mode: str = ""
	status: str = "disconnected"
	detail: str = ""
	qr_text: str = ""
	expires_at: str = ""
	account_name: str = ""
	session_id: str = ""
	raw: dict[str, Any] = field(default_factory=dict)

	def to_config_patch(self) -> dict[str, str]:
		patch = {
			"connection_mode": str(self.mode or ""),
			"bridge_session_id": str(self.session_id or ""),
			"qr_login_url": str(self.qr_text or ""),
			"qr_status": str(self.status or ""),
			"qr_expires_at": str(self.expires_at or ""),
			"connected_account": str(self.account_name or ""),
			"status_detail": str(self.detail or ""),
		}
		for key in ("bridge_api_base", "bridge_bot_token", "bridge_bot_id", "bridge_user_id"):
			if key in self.raw:
				patch[key] = str(self.raw.get(key, "") or "")
		return patch


@dataclass(frozen=True)
class ChannelConversationSummary:
	conversation_id: str
	title: str
	updated_at: float = 0.0
	preview: str = ""
	participant_label: str = ""
	is_manual_test_session: bool = False
	is_primary_session: bool = False


@dataclass(frozen=True)
class ChannelRuntimeEvent:
	kind: str
	channel_id: str
	conversation_id: str = ""
	source: str = ""
	focus_requested: bool = False
	request_id: str = ""
	payload: dict[str, Any] = field(default_factory=dict)


class ChannelConversationBindingStore:
	def __init__(self, path: Path | None = None) -> None:
		self._path = path or (get_global_data_dir() / "channel_bindings.json")
		self._lock = threading.Lock()
		self._data = self._load()

	def _load(self) -> dict[str, str]:
		try:
			if self._path.exists() and self._path.is_file():
				raw = json.loads(self._path.read_text(encoding="utf-8"))
				if isinstance(raw, dict):
					return {
						str(key): str(value)
						for key, value in raw.items()
						if str(key).strip() and str(value).strip()
					}
		except Exception as exc:
			logger.debug("Failed to load channel binding store: %s", exc)
		return {}

	def get(self, channel_id: str, user_id: str) -> str:
		key = self._make_key(channel_id, user_id)
		with self._lock:
			return str(self._data.get(key, "") or "")

	def set(self, channel_id: str, user_id: str, conversation_id: str) -> None:
		key = self._make_key(channel_id, user_id)
		value = str(conversation_id or "").strip()
		if not key or not value:
			return
		with self._lock:
			self._data[key] = value
			self._save_locked()

	def items(self) -> dict[str, str]:
		with self._lock:
			return dict(self._data)

	def remove(self, channel_id: str, user_id: str) -> bool:
		key = self._make_key(channel_id, user_id)
		if not key:
			return False
		with self._lock:
			if key not in self._data:
				return False
			del self._data[key]
			self._save_locked()
			return True

	def prune(
		self,
		*,
		valid_channel_ids: set[str] | None = None,
		valid_conversation_ids: set[str] | None = None,
	) -> int:
		channels = {str(item or "").strip() for item in (valid_channel_ids or set()) if str(item or "").strip()}
		conversations = {str(item or "").strip() for item in (valid_conversation_ids or set()) if str(item or "").strip()}
		removed = 0
		with self._lock:
			for key, value in list(self._data.items()):
				channel_id = str(key).split("::", 1)[0].strip()
				conversation_id = str(value or "").strip()
				if channels and channel_id not in channels:
					del self._data[key]
					removed += 1
					continue
				if conversations and conversation_id not in conversations:
					del self._data[key]
					removed += 1
			if removed:
				self._save_locked()
		return removed

	@staticmethod
	def _make_key(channel_id: str, user_id: str) -> str:
		return f"{str(channel_id or '').strip()}::{str(user_id or '').strip()}"

	def _save_locked(self) -> None:
		try:
			self._path.parent.mkdir(parents=True, exist_ok=True)
			self._path.write_text(
				json.dumps(self._data, ensure_ascii=False, indent=2),
				encoding="utf-8",
			)
		except Exception as exc:
			logger.debug("Failed to save channel binding store: %s", exc)


__all__ = [
	"ChannelServerHandle",
	"ChannelConnectionSnapshot",
	"ChannelConversationBindingStore",
	"ChannelConversationSummary",
	"ChannelRuntimeEvent",
	"_WeChatBridgePollerHandle",
	"_WeChatServerHandle",
]
