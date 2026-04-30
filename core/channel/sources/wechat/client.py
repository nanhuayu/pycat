from __future__ import annotations

import httpx
import time
import uuid
from typing import Any
from urllib.parse import urlparse

from core.channel.models import ChannelConnectionSnapshot
from core.channel.sources.wechat.protocol import normalize_wechat_reply_text
from core.config.schema import ChannelConfig


WECHAT_CHERRY_BRIDGE_BASE = "https://ilinkai.weixin.qq.com"
WECHAT_CHERRY_CHANNEL_VERSION = "1.0.0"
WECHAT_BRIDGE_MESSAGE_BOT = 2
WECHAT_BRIDGE_ITEM_TEXT = 1


class WeChatChannelClient:
    def __init__(self) -> None:
        self._access_token_cache: dict[str, tuple[str, float]] = {}

    def build_qr_snapshot_from_config(self, channel: ChannelConfig) -> ChannelConnectionSnapshot:
        config = dict(getattr(channel, "config", {}) or {})
        status = self.normalize_qr_status(config.get("qr_status", "disconnected"))
        session_id = str(config.get("bridge_session_id", "") or "").strip()
        qr_text = str(config.get("qr_login_url", "") or "").strip()
        account_name = str(config.get("connected_account", "") or "").strip()
        expires_at = str(config.get("qr_expires_at", "") or "").strip()
        detail = str(config.get("status_detail", "") or "").strip()

        if not detail:
            if status in {"connected", "ready"}:
                detail = f"已连接 {account_name}" if account_name else "扫码连接已完成。"
            elif status == "scanned":
                detail = "二维码已扫描，请在手机上确认登录。"
            elif status in {"pending", "waiting"}:
                detail = "二维码已生成，等待扫码。"
            elif status == "expired":
                detail = "二维码已失效，请重新生成。"
            elif status == "error":
                detail = "二维码连接失败，请检查桥接地址或重新生成。"
            elif qr_text:
                detail = "二维码已生成，等待扫码。"
            else:
                detail = "尚未生成二维码，可点击下方按钮创建扫码连接。"

        return ChannelConnectionSnapshot(
            channel_id=str(getattr(channel, "id", "") or ""),
            channel_type="wechat",
            mode="qr-bridge",
            status=status,
            detail=detail,
            qr_text=qr_text,
            expires_at=expires_at,
            account_name=account_name,
            session_id=session_id,
            raw=config,
        )

    def create_qr_bridge_session(self, channel: ChannelConfig, *, bridge_api_base: str) -> dict[str, Any]:
        if self.wechat_bridge_uses_cherry_protocol(bridge_api_base):
            return self.create_cherry_bridge_session(channel, bridge_api_base=bridge_api_base)
        return self.create_session_api_bridge_session(channel, bridge_api_base=bridge_api_base)

    def create_session_api_bridge_session(self, channel: ChannelConfig, *, bridge_api_base: str) -> dict[str, Any]:
        payload = {
            "channel_id": str(getattr(channel, "id", "") or "").strip(),
            "channel_type": "wechat",
            "source": str(getattr(channel, "source", "") or "").strip(),
            "name": str(getattr(channel, "name", "") or "").strip(),
            "allow_tools": bool(getattr(channel, "allow_tools", True)),
        }
        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                f"{bridge_api_base}/sessions",
                json=payload,
                headers=self.bridge_headers(channel),
            )
            response.raise_for_status()
            raw = self.read_bridge_json(response, operation="create QR bridge session")
        return raw if isinstance(raw, dict) else {}

    def create_cherry_bridge_session(self, channel: ChannelConfig, *, bridge_api_base: str) -> dict[str, Any]:
        with httpx.Client(timeout=20.0) as client:
            response = client.get(
                f"{bridge_api_base}/ilink/bot/get_bot_qrcode",
                params={"bot_type": "3"},
                headers=self.bridge_headers(channel),
            )
            response.raise_for_status()
            raw = self.read_bridge_json(response, operation="create Cherry WeChat QR session")

        if isinstance(raw, dict):
            raw.setdefault("status", "wait")
        return raw if isinstance(raw, dict) else {}

    def fetch_qr_bridge_session(
        self,
        channel: ChannelConfig,
        *,
        bridge_api_base: str,
        session_id: str,
    ) -> dict[str, Any]:
        if self.wechat_bridge_uses_cherry_protocol(bridge_api_base):
            return self.fetch_cherry_bridge_session(
                channel,
                bridge_api_base=bridge_api_base,
                session_id=session_id,
            )
        return self.fetch_session_api_bridge_session(
            channel,
            bridge_api_base=bridge_api_base,
            session_id=session_id,
        )

    def fetch_session_api_bridge_session(
        self,
        channel: ChannelConfig,
        *,
        bridge_api_base: str,
        session_id: str,
    ) -> dict[str, Any]:
        with httpx.Client(timeout=20.0) as client:
            response = client.get(
                f"{bridge_api_base}/sessions/{session_id}",
                headers=self.bridge_headers(channel),
            )
            response.raise_for_status()
            raw = self.read_bridge_json(response, operation="fetch QR bridge session")
        return raw if isinstance(raw, dict) else {}

    def fetch_cherry_bridge_session(
        self,
        channel: ChannelConfig,
        *,
        bridge_api_base: str,
        session_id: str,
    ) -> dict[str, Any]:
        headers = self.bridge_headers(channel)
        headers["iLink-App-ClientVersion"] = "1"
        with httpx.Client(timeout=20.0) as client:
            response = client.get(
                f"{bridge_api_base}/ilink/bot/get_qrcode_status",
                params={"qrcode": session_id},
                headers=headers,
            )
            response.raise_for_status()
            raw = self.read_bridge_json(response, operation="fetch Cherry WeChat QR status")

        if isinstance(raw, dict):
            raw.setdefault("qrcode", session_id)
        return raw if isinstance(raw, dict) else {}

    def snapshot_from_qr_payload(
        self,
        channel: ChannelConfig,
        payload: dict[str, Any],
        *,
        fallback: dict[str, Any] | None = None,
    ) -> ChannelConnectionSnapshot:
        merged: dict[str, Any] = {}
        payload_fields: dict[str, Any] = {}
        if isinstance(fallback, dict):
            merged.update(fallback)
        if isinstance(payload, dict):
            payload_fields.update(payload)
            for key in ("data", "session", "result"):
                nested = payload.get(key)
                if isinstance(nested, dict):
                    payload_fields.update(nested)
        merged.update(payload_fields)

        qr_text = self.coalesce_text(
            payload_fields,
            "qr_text",
            "qrText",
            "qr_url",
            "qrUrl",
            "login_url",
            "loginUrl",
            "code_url",
            "codeUrl",
            "qrcode_img_content",
            default=self.coalesce_text(
                merged,
                "qr_text",
                "qrText",
                "qr_url",
                "qrUrl",
                "login_url",
                "loginUrl",
                "code_url",
                "codeUrl",
                "qrcode_img_content",
                default="",
            ),
        )
        session_id = self.coalesce_text(
            payload_fields,
            "bridge_session_id",
            "session_id",
            "sessionId",
            "qrcode",
            "id",
            default=self.coalesce_text(
                merged,
                "bridge_session_id",
                "session_id",
                "sessionId",
                "qrcode",
                "id",
                default="",
            ),
        )
        status = self.normalize_qr_status(
            self.coalesce_text(
                payload_fields,
                "status",
                "state",
                "qr_status",
                default=self.coalesce_text(
                    merged,
                    "status",
                    "state",
                    "qr_status",
                    default="pending",
                ),
            )
        )
        expires_at = self.coalesce_text(
            payload_fields,
            "expires_at",
            "expiresAt",
            "qr_expires_at",
            default=self.coalesce_text(
                merged,
                "expires_at",
                "expiresAt",
                "qr_expires_at",
                default="",
            ),
        )
        account_name = self.coalesce_text(
            payload_fields,
            "connected_account",
            "account_name",
            "accountName",
            "account",
            "ilink_user_id",
            "user_id",
            default=self.coalesce_text(
                merged,
                "connected_account",
                "account_name",
                "accountName",
                "account",
                "ilink_user_id",
                "user_id",
                default="",
            ),
        )
        detail = self.coalesce_text(
            payload_fields,
            "status_detail",
            "detail",
            "message",
            "errmsg",
            default=self.coalesce_text(
                merged,
                "status_detail",
                "detail",
                "message",
                "errmsg",
                default="",
            ),
        )
        bridge_api_base = self.normalize_bridge_base_url(
            self.coalesce_text(
                payload_fields,
                "bridge_api_base",
                "baseurl",
                default=self.coalesce_text(
                    merged,
                    "bridge_api_base",
                    "baseurl",
                    default=WECHAT_CHERRY_BRIDGE_BASE,
                ),
            )
        )
        bridge_bot_token = self.coalesce_text(
            payload_fields,
            "bridge_bot_token",
            "bot_token",
            default=self.coalesce_text(
                merged,
                "bridge_bot_token",
                "bot_token",
                default="",
            ),
        )
        bridge_bot_id = self.coalesce_text(
            payload_fields,
            "bridge_bot_id",
            "ilink_bot_id",
            default=self.coalesce_text(
                merged,
                "bridge_bot_id",
                "ilink_bot_id",
                default="",
            ),
        )
        bridge_user_id = self.coalesce_text(
            payload_fields,
            "bridge_user_id",
            "ilink_user_id",
            default=self.coalesce_text(
                merged,
                "bridge_user_id",
                "ilink_user_id",
                default="",
            ),
        )

        if status not in {"connected", "ready"}:
            if not self.coalesce_text(payload_fields, "bot_token", "bridge_bot_token"):
                bridge_bot_token = ""
            if not self.coalesce_text(payload_fields, "ilink_bot_id", "bridge_bot_id"):
                bridge_bot_id = ""
            if not self.coalesce_text(payload_fields, "ilink_user_id", "bridge_user_id"):
                bridge_user_id = ""
            if not self.coalesce_text(payload_fields, "connected_account", "account_name", "account", "ilink_user_id"):
                account_name = ""

        config = dict(getattr(channel, "config", {}) or {})
        config.update(
            {
                "connection_mode": "qr-bridge",
                "bridge_api_base": bridge_api_base,
                "bridge_session_id": session_id,
                "bridge_bot_token": bridge_bot_token,
                "bridge_bot_id": bridge_bot_id,
                "bridge_user_id": bridge_user_id,
                "qr_login_url": qr_text,
                "qr_status": status,
                "qr_expires_at": expires_at,
                "connected_account": account_name,
                "status_detail": detail,
            }
        )

        updated_channel = ChannelConfig(
            id=channel.id,
            name=channel.name,
            type=channel.type,
            enabled=channel.enabled,
            allow_tools=channel.allow_tools,
            source=channel.source,
            description=channel.description,
            agent_id=channel.agent_id,
            session_id=channel.session_id,
            permission_mode=channel.permission_mode,
            status=channel.status,
            created_at=channel.created_at,
            updated_at=channel.updated_at,
            webhook_url=channel.webhook_url,
            token=channel.token,
            secret=channel.secret,
            config=config,
        )
        return self.build_qr_snapshot_from_config(updated_channel)

    def fetch_bridge_updates(
        self,
        *,
        base_url: str,
        bot_token: str,
        uin: str,
        cursor: str,
    ) -> dict[str, Any]:
        payload = {
            "get_updates_buf": str(cursor or ""),
            "base_info": self.build_bridge_base_info(),
        }
        with httpx.Client(timeout=45.0) as client:
            response = client.post(
                f"{self.normalize_bridge_base_url(base_url)}/ilink/bot/getupdates",
                json=payload,
                headers=self.wechat_bridge_bot_headers(bot_token, uin),
            )
            response.raise_for_status()
            raw = self.read_bridge_json(response, operation="fetch Cherry WeChat updates")
        return raw if isinstance(raw, dict) else {}

    @staticmethod
    def is_bridge_reauth_required(exc: Exception) -> bool:
        if isinstance(exc, httpx.HTTPStatusError):
            response = getattr(exc, "response", None)
            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code in {401, 403}:
                return True

        text = str(exc or "").strip().lower()
        return "session expired" in text or "login expired" in text or "invalid token" in text

    @staticmethod
    def is_bridge_transient_poll_error(exc: Exception) -> bool:
        if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError)):
            return True
        text = str(exc or "").strip().lower()
        return "timed out" in text or "timeout" in text or "temporarily unavailable" in text

    def send_official_reply(self, access_token: str, *, touser: str, content: str) -> None:
        url = f"https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token={access_token}"
        payload = {
            "touser": touser,
            "msgtype": "text",
            "text": {"content": normalize_wechat_reply_text(content)},
        }
        with httpx.Client(timeout=20.0) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
        if int(data.get("errcode", 0) or 0) != 0:
            raise RuntimeError(f"微信客服接口返回错误: {data.get('errmsg') or data}")

    def send_bridge_reply(
        self,
        channel: ChannelConfig,
        *,
        touser: str,
        content: str,
        context_token: str = "",
        bridge_uin: str = "",
    ) -> None:
        credentials = self.resolve_bridge_credentials(channel)
        if credentials is None:
            raise RuntimeError("微信二维码桥接尚未完成登录，无法回发消息。")
        if not touser:
            raise RuntimeError("微信二维码桥接缺少目标用户，无法回发消息。")

        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": str(touser or "").strip(),
                "client_id": str(uuid.uuid4()),
                "message_type": WECHAT_BRIDGE_MESSAGE_BOT,
                "message_state": 2,
                "context_token": str(context_token or "").strip(),
                "item_list": [
                    {
                        "type": WECHAT_BRIDGE_ITEM_TEXT,
                        "text_item": {"text": normalize_wechat_reply_text(content)},
                    }
                ],
            },
            "base_info": self.build_bridge_base_info(),
        }

        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                f"{credentials['base_url']}/ilink/bot/sendmessage",
                json=payload,
                headers=self.wechat_bridge_bot_headers(credentials["bot_token"], bridge_uin),
            )
            response.raise_for_status()
            self.read_bridge_json(response, operation="send Cherry WeChat reply")

    def get_access_token(self, channel: ChannelConfig, *, app_id: str, app_secret: str) -> str:
        cache_key = str(channel.id or "").strip() or f"wechat::{app_id}"
        cached = self._access_token_cache.get(cache_key)
        current = time.time()
        if cached and cached[0] and cached[1] > current + 60:
            return cached[0]

        url = "https://api.weixin.qq.com/cgi-bin/token"
        params = {
            "grant_type": "client_credential",
            "appid": app_id,
            "secret": app_secret,
        }
        with httpx.Client(timeout=20.0) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

        token = str(data.get("access_token", "") or "").strip()
        if not token:
            raise RuntimeError(f"获取微信 access_token 失败: {data.get('errmsg') or data}")
        expires_in = int(data.get("expires_in", 7200) or 7200)
        self._access_token_cache[cache_key] = (token, current + max(300, expires_in))
        return token

    def resolve_bridge_credentials(self, channel: ChannelConfig) -> dict[str, str] | None:
        config = dict(getattr(channel, "config", {}) or {})
        bot_token = str(config.get("bridge_bot_token", "") or "").strip()
        if not bot_token:
            return None
        return {
            "base_url": self.normalize_bridge_base_url(config.get("bridge_api_base", WECHAT_CHERRY_BRIDGE_BASE)),
            "bot_token": bot_token,
            "bot_id": str(config.get("bridge_bot_id", "") or "").strip(),
            "user_id": str(config.get("bridge_user_id", "") or "").strip(),
        }

    @staticmethod
    def bridge_headers(channel: ChannelConfig) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        config = dict(getattr(channel, "config", {}) or {})
        token = str(config.get("bridge_token", "") or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def normalize_qr_status(value: Any) -> str:
        raw = str(value or "").strip().lower()
        mapping = {
            "wait": "pending",
            "pending": "pending",
            "waiting": "pending",
            "scaned": "scanned",
            "scanned": "scanned",
            "confirmed": "connected",
            "connected": "connected",
            "ready": "connected",
            "expired": "expired",
            "error": "error",
            "disconnected": "disconnected",
            "draft": "draft",
        }
        return mapping.get(raw, raw or "disconnected")

    @staticmethod
    def wechat_bridge_uses_cherry_protocol(bridge_api_base: str) -> bool:
        normalized = str(bridge_api_base or "").strip().rstrip("/")
        if not normalized:
            return False
        parsed = urlparse(normalized)
        host = str(parsed.netloc or "").strip().lower()
        path = str(parsed.path or "").strip().rstrip("/").lower()
        if host.endswith("weixin.qq.com"):
            return True
        if not path:
            return True
        return not path.endswith("/api/wechat")

    @staticmethod
    def read_bridge_json(response: httpx.Response, *, operation: str) -> dict[str, Any]:
        raw = response.json()
        if not isinstance(raw, dict):
            return {}
        ret = raw.get("ret")
        errcode = raw.get("errcode")
        if isinstance(ret, int) and ret != 0:
            raise RuntimeError(str(raw.get("errmsg") or f"{operation} 失败"))
        if isinstance(errcode, int) and errcode != 0:
            raise RuntimeError(str(raw.get("errmsg") or f"{operation} 失败"))
        return raw

    @staticmethod
    def coalesce_text(mapping: dict[str, Any], *keys: str, default: str = "") -> str:
        for key in keys:
            value = mapping.get(key)
            text = str(value or "").strip()
            if text:
                return text
        return str(default or "").strip()

    @staticmethod
    def normalize_bridge_base_url(value: Any) -> str:
        normalized = str(value or WECHAT_CHERRY_BRIDGE_BASE).strip().rstrip("/")
        return normalized or WECHAT_CHERRY_BRIDGE_BASE

    @staticmethod
    def build_bridge_base_info() -> dict[str, str]:
        return {"channel_version": WECHAT_CHERRY_CHANNEL_VERSION}

    @staticmethod
    def wechat_bridge_bot_headers(bot_token: str, uin: str) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {str(bot_token or '').strip()}",
            "X-WECHAT-UIN": str(uin or "").strip(),
        }


__all__ = ["WECHAT_CHERRY_BRIDGE_BASE", "WeChatChannelClient"]