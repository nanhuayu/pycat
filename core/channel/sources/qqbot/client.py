from __future__ import annotations

import time
from typing import Any

import httpx

from core.config.schema import ChannelConfig


QQBOT_OPEN_BASE = "https://api.sgroup.qq.com"
QQBOT_SANDBOX_OPEN_BASE = "https://sandbox.api.sgroup.qq.com"
QQBOT_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"


class QQBotChannelClient:
    def __init__(self) -> None:
        self._token_cache: dict[str, tuple[str, float]] = {}

    def send_text_message(
        self,
        channel: ChannelConfig,
        *,
        receive_id: str,
        text: str,
        context_token: str = "",
    ) -> None:
        config = dict(getattr(channel, "config", {}) or {})
        target_id = str(receive_id or "").strip() or str(config.get("target_id", "") or "").strip()
        if not target_id:
            raise RuntimeError("QQ Bot 缺少入站事件中的 openid / group_openid / channel_id，无法回发消息。")

        endpoint = self.resolve_send_endpoint(config, target_id=target_id)
        payload = self.build_message_payload(
            config,
            text=text,
            context_token=context_token,
        )

        headers = {"Content-Type": "application/json; charset=utf-8"}
        token = self.resolve_access_token(channel, config)
        if token:
            headers["Authorization"] = f"QQBot {token}"

        with httpx.Client(timeout=20.0) as client:
            response = client.post(endpoint, json=payload, headers=headers)
            response.raise_for_status()
            try:
                data = response.json()
            except Exception:
                data = {}

        if isinstance(data, dict):
            code = data.get("code", data.get("retcode", data.get("errcode", 0)))
            try:
                code_value = int(code or 0)
            except Exception:
                code_value = 0
            if code_value != 0:
                raise RuntimeError(f"QQ Bot 消息发送失败: {data.get('message') or data.get('msg') or data}")

    def get_gateway_url(self, channel: ChannelConfig, *, access_token: str = "") -> str:
        config = dict(getattr(channel, "config", {}) or {})
        token = str(access_token or "").strip() or self.resolve_access_token(channel, config)
        base = self.normalize_open_base_url(
            config.get("api_base_url", "") or (QQBOT_SANDBOX_OPEN_BASE if _truthy(config.get("sandbox")) else QQBOT_OPEN_BASE)
        )
        headers = {"Authorization": f"QQBot {token}", "Content-Type": "application/json; charset=utf-8"}
        with httpx.Client(timeout=20.0) as client:
            response = client.get(f"{base}/gateway", headers=headers)
            response.raise_for_status()
            data = response.json()

        if isinstance(data, dict):
            code = data.get("code", data.get("retcode", data.get("errcode", 0)))
            try:
                code_value = int(code or 0)
            except Exception:
                code_value = 0
            if code_value != 0:
                raise RuntimeError(f"QQ Bot 获取 Gateway 地址失败: {data.get('message') or data.get('msg') or data}")

        url = str((data or {}).get("url", "") or "").strip() if isinstance(data, dict) else ""
        if not url:
            raise RuntimeError(f"QQ Bot 获取 Gateway 地址失败: {data}")
        return url

    def resolve_access_token(self, channel: ChannelConfig, config: dict[str, Any]) -> str:
        configured = str(config.get("access_token", "") or "").strip()
        if configured:
            return configured

        app_id = str(config.get("app_id", "") or config.get("appId", "") or config.get("appid", "") or "").strip()
        app_secret = str(
            config.get("app_secret", "")
            or config.get("appSecret", "")
            or config.get("appsecret", "")
            or config.get("client_secret", "")
            or config.get("clientSecret", "")
            or config.get("secret", "")
            or ""
        ).strip()
        if not (app_id and app_secret):
            raise RuntimeError("QQ Bot 缺少 app_id / app_secret，无法获取 AccessToken。")

        cache_key = str(getattr(channel, "id", "") or "").strip() or f"qqbot::{app_id}"
        cached = self._token_cache.get(cache_key)
        if cached and cached[1] > time.time() + 60:
            return cached[0]

        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                QQBOT_TOKEN_URL,
                json={"appId": app_id, "clientSecret": app_secret},
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            response.raise_for_status()
            data = response.json()

        access_token = str(data.get("access_token", "") or "").strip()
        if not access_token:
            raise RuntimeError(f"QQ Bot 获取 AccessToken 失败: {data}")
        try:
            expires_in = int(data.get("expires_in", 7200) or 7200)
        except Exception:
            expires_in = 7200
        self._token_cache[cache_key] = (access_token, time.time() + max(60, expires_in - 120))
        return access_token

    @classmethod
    def resolve_send_endpoint(cls, config: dict[str, Any], *, target_id: str) -> str:
        explicit = str(config.get("send_endpoint", "") or "").strip()
        if explicit:
            return explicit.replace("{target_id}", str(target_id or "").strip())

        base = cls.normalize_open_base_url(config.get("api_base_url", "") or (QQBOT_SANDBOX_OPEN_BASE if _truthy(config.get("sandbox")) else QQBOT_OPEN_BASE))
        target_type = str(config.get("target_type", "channel") or "channel").strip().lower() or "channel"
        if target_type in {"group", "group_openid"}:
            return f"{base}/v2/groups/{target_id}/messages"
        if target_type in {"user", "openid", "private", "c2c"}:
            return f"{base}/v2/users/{target_id}/messages"
        if target_type in {"dm", "dms", "direct", "guild_dm", "guild"}:
            return f"{base}/dms/{target_id}/messages"
        return f"{base}/channels/{target_id}/messages"

    @classmethod
    def build_message_payload(cls, config: dict[str, Any], *, text: Any, context_token: str = "") -> dict[str, Any]:
        target_type = str(config.get("target_type", "channel") or "channel").strip().lower() or "channel"
        content = cls.normalize_reply_text(text)
        token = str(context_token or "").strip()
        if target_type in {"group", "group_openid", "user", "openid", "private", "c2c"}:
            payload: dict[str, Any] = {
                "content": content,
                "msg_type": 0,
                "msg_seq": cls.next_msg_seq(token),
            }
        else:
            payload = {"content": content}
        if token:
            payload["msg_id"] = token
        return payload

    @staticmethod
    def next_msg_seq(seed: Any = "") -> int:
        seed_text = str(seed or "")
        seed_value = sum(ord(ch) for ch in seed_text) if seed_text else 0
        return max(1, int((int(time.time() * 1000) ^ seed_value) % 65536))

    @staticmethod
    def normalize_open_base_url(value: Any) -> str:
        normalized = str(value or QQBOT_OPEN_BASE).strip().rstrip("/")
        return normalized or QQBOT_OPEN_BASE

    @staticmethod
    def normalize_reply_text(content: Any, *, limit: int = 2000) -> str:
        text = str(content or "").replace("\r\n", "\n").strip()
        if not text:
            return "已收到消息，但暂时没有可发送的文本回复。"
        if len(text) <= limit:
            return text
        return text[: max(1, limit - 1)].rstrip() + "…"


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "sandbox"}


__all__ = ["QQBOT_OPEN_BASE", "QQBOT_SANDBOX_OPEN_BASE", "QQBOT_TOKEN_URL", "QQBotChannelClient"]