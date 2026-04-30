from __future__ import annotations

from typing import Any

import httpx

from core.config.schema import ChannelConfig


TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_MESSAGE_LIMIT = 4096


class TelegramChannelClient:
    """Small Telegram Bot API client used by the channel runtime."""

    def get_updates(
        self,
        channel: ChannelConfig,
        *,
        offset: int = 0,
        timeout: int | None = None,
    ) -> list[dict[str, Any]]:
        config = dict(getattr(channel, "config", {}) or {})
        poll_timeout = self.resolve_poll_timeout(config, timeout=timeout)
        payload: dict[str, Any] = {
            "timeout": poll_timeout,
            "limit": 50,
        }
        if int(offset or 0) > 0:
            payload["offset"] = int(offset or 0)
        allowed_updates = self.normalize_allowed_updates(config.get("allowed_updates", ""))
        if allowed_updates:
            payload["allowed_updates"] = list(allowed_updates)

        with self.open_http_client(config, timeout=max(10.0, float(poll_timeout) + 10.0)) as client:
            response = client.post(self.build_api_url(channel, "getUpdates"), json=payload)
            response.raise_for_status()
            result = self.read_api_result(response.json(), operation="getUpdates")

        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        return []

    def send_text_message(
        self,
        channel: ChannelConfig,
        *,
        chat_id: str,
        text: Any,
        context_token: str = "",
        message_thread_id: str = "",
    ) -> dict[str, Any]:
        config = dict(getattr(channel, "config", {}) or {})
        resolved_chat_id = str(chat_id or config.get("chat_id", "") or "").strip()
        if not resolved_chat_id:
            raise RuntimeError("Telegram 频道缺少 Chat ID，无法回发消息。")

        payload: dict[str, Any] = {
            "chat_id": resolved_chat_id,
            "text": self.normalize_reply_text(text),
        }
        parse_mode = str(config.get("parse_mode", "") or "").strip()
        if parse_mode:
            payload["parse_mode"] = parse_mode

        thread_id = _to_int(message_thread_id)
        if thread_id is not None:
            payload["message_thread_id"] = thread_id

        if _truthy(config.get("reply_to_message")):
            reply_message_id = _to_int(context_token)
            if reply_message_id is not None:
                payload["reply_parameters"] = {
                    "message_id": reply_message_id,
                    "allow_sending_without_reply": True,
                }

        with self.open_http_client(config, timeout=20.0) as client:
            response = client.post(self.build_api_url(channel, "sendMessage"), json=payload)
            response.raise_for_status()
            result = self.read_api_result(response.json(), operation="sendMessage")

        return result if isinstance(result, dict) else {}

    def build_api_url(self, channel: ChannelConfig, method: str) -> str:
        config = dict(getattr(channel, "config", {}) or {})
        token = str(config.get("bot_token", "") or config.get("token", "") or "").strip()
        if not token:
            raise RuntimeError("Telegram 频道缺少 Bot Token，无法调用 Bot API。")
        api_base = self.normalize_api_base_url(config.get("api_base_url", TELEGRAM_API_BASE))
        method_name = str(method or "").strip().lstrip("/")
        if not method_name:
            raise RuntimeError("Telegram Bot API 方法名为空。")
        return f"{api_base}/bot{token}/{method_name}"

    @classmethod
    def open_http_client(cls, config: dict[str, Any], *, timeout: float) -> httpx.Client:
        proxy_url = str(config.get("proxy_url", "") or "").strip()
        options: dict[str, Any] = {"timeout": float(timeout or 20.0)}
        if proxy_url:
            options["proxy"] = proxy_url
        try:
            return httpx.Client(**options)
        except TypeError:
            if proxy_url:
                options.pop("proxy", None)
                options["proxies"] = proxy_url
                return httpx.Client(**options)
            raise

    @staticmethod
    def read_api_result(payload: Any, *, operation: str) -> Any:
        if not isinstance(payload, dict):
            raise RuntimeError(f"Telegram {operation} 返回格式无效: {payload}")
        if payload.get("ok") is False:
            raise RuntimeError(f"Telegram {operation} 失败: {payload.get('description') or payload}")
        if "ok" in payload and not bool(payload.get("ok")):
            raise RuntimeError(f"Telegram {operation} 失败: {payload.get('description') or payload}")
        return payload.get("result")

    @staticmethod
    def normalize_api_base_url(value: Any) -> str:
        normalized = str(value or TELEGRAM_API_BASE).strip().rstrip("/")
        return normalized or TELEGRAM_API_BASE

    @staticmethod
    def resolve_poll_timeout(config: dict[str, Any], *, timeout: int | None = None) -> int:
        raw_value = timeout if timeout is not None else config.get("poll_timeout", 25)
        try:
            value = int(str(raw_value or "25").strip())
        except Exception:
            value = 25
        return min(50, max(1, value))

    @staticmethod
    def normalize_allowed_updates(value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            candidates = [part.strip() for part in value.replace("\n", ",").split(",")]
        elif isinstance(value, (list, tuple, set)):
            candidates = [str(item or "").strip() for item in value]
        else:
            candidates = []

        seen: set[str] = set()
        normalized: list[str] = []
        for item in candidates:
            if not item or item in seen:
                continue
            seen.add(item)
            normalized.append(item)
        return tuple(normalized)

    @staticmethod
    def normalize_reply_text(content: Any, *, limit: int = TELEGRAM_MESSAGE_LIMIT) -> str:
        text = str(content or "").replace("\r\n", "\n").strip()
        if not text:
            return "已收到消息，但暂时没有可发送的文本回复。"
        max_len = max(1, int(limit or TELEGRAM_MESSAGE_LIMIT))
        if len(text) <= max_len:
            return text
        return text[: max(1, max_len - 1)].rstrip() + "…"


def _to_int(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except Exception:
        return None


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


__all__ = ["TELEGRAM_API_BASE", "TELEGRAM_MESSAGE_LIMIT", "TelegramChannelClient"]