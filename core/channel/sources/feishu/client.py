from __future__ import annotations

import json
import time
from typing import Any

import httpx

from core.config.schema import ChannelConfig


FEISHU_OPEN_BASE = "https://open.feishu.cn"


class FeishuChannelClient:
    def __init__(self) -> None:
        self._tenant_access_token_cache: dict[str, tuple[str, float]] = {}

    def send_text_message(
        self,
        channel: ChannelConfig,
        *,
        receive_id: str,
        text: str,
        receive_id_type: str = "chat_id",
    ) -> None:
        config = dict(getattr(channel, "config", {}) or {})
        app_id = str(config.get("app_id", "") or "").strip()
        app_secret = str(config.get("app_secret", "") or "").strip()
        if not (app_id and app_secret and str(receive_id or "").strip()):
            raise RuntimeError("飞书频道缺少 app_id / app_secret / receive_id，无法回发消息。")

        base_url = self.normalize_open_base_url(config.get("open_base_url", FEISHU_OPEN_BASE))
        access_token = self.get_tenant_access_token(
            channel,
            app_id=app_id,
            app_secret=app_secret,
            open_base_url=base_url,
        )
        payload = {
            "receive_id": str(receive_id or "").strip(),
            "msg_type": "text",
            "content": json.dumps({"text": self.normalize_reply_text(text)}, ensure_ascii=False),
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                f"{base_url}/open-apis/im/v1/messages",
                params={"receive_id_type": str(receive_id_type or "chat_id").strip() or "chat_id"},
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()

        code = int(data.get("code", 0) or 0)
        if code != 0:
            raise RuntimeError(f"飞书消息发送失败: {data.get('msg') or data.get('message') or data}")

    def get_tenant_access_token(
        self,
        channel: ChannelConfig,
        *,
        app_id: str,
        app_secret: str,
        open_base_url: str = FEISHU_OPEN_BASE,
    ) -> str:
        cache_key = str(channel.id or "").strip() or f"feishu::{app_id}"
        cached = self._tenant_access_token_cache.get(cache_key)
        current = time.time()
        if cached and cached[0] and cached[1] > current + 60:
            return cached[0]

        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                f"{self.normalize_open_base_url(open_base_url)}/open-apis/auth/v3/tenant_access_token/internal",
                json={
                    "app_id": str(app_id or "").strip(),
                    "app_secret": str(app_secret or "").strip(),
                },
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            response.raise_for_status()
            data = response.json()

        code = int(data.get("code", 0) or 0)
        token = str(data.get("tenant_access_token", "") or "").strip()
        if code != 0 or not token:
            raise RuntimeError(f"获取飞书 tenant_access_token 失败: {data.get('msg') or data.get('message') or data}")

        expire = int(data.get("expire", 7200) or 7200)
        self._tenant_access_token_cache[cache_key] = (token, current + max(300, expire))
        return token

    @staticmethod
    def normalize_open_base_url(value: Any) -> str:
        normalized = str(value or FEISHU_OPEN_BASE).strip().rstrip("/")
        return normalized or FEISHU_OPEN_BASE

    @staticmethod
    def normalize_reply_text(content: Any, *, limit: int = 4000) -> str:
        text = str(content or "").replace("\r\n", "\n").strip()
        if not text:
            return "已收到消息，但暂时没有可发送的文本回复。"
        if len(text) <= limit:
            return text
        return text[: max(1, limit - 1)].rstrip() + "…"


__all__ = ["FEISHU_OPEN_BASE", "FeishuChannelClient"]