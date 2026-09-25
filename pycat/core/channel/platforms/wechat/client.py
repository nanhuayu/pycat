from __future__ import annotations

import base64
import secrets
import time
import uuid
from typing import Any, Mapping

import httpx

from pycat.core.channel.platforms.wechat.protocol import normalize_wechat_reply_text
from pycat.core.version import __version__ as PYCAT_VERSION
from pycat.models.contracts.channel import ChannelConfig

WECHAT_ILINK_LOGIN_BASE = "https://ilinkai.weixin.qq.com"
WECHAT_ILINK_APP_ID = "bot"
PYCAT_BOT_AGENT = f"PyCat/{PYCAT_VERSION}"
WECHAT_MESSAGE_BOT = 2
WECHAT_MESSAGE_FINISH = 2
WECHAT_ITEM_TEXT = 1
WECHAT_QR_CREATE_TIMEOUT = httpx.Timeout(connect=15.0, read=None, write=15.0, pool=15.0)


class WeChatAPIError(RuntimeError):
    def __init__(self, message: str, *, code: int = 0) -> None:
        super().__init__(message)
        self.code = int(code or 0)


def encode_client_version(version: str) -> int:
    parts = []
    for item in str(version or "").split(".")[:3]:
        try:
            parts.append(int(item))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts
    return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)


def ilink_headers(*, token: str = "", uin: str = "") -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": str(uin or "").strip() or random_wechat_uin(),
        "iLink-App-Id": WECHAT_ILINK_APP_ID,
        "iLink-App-ClientVersion": str(encode_client_version(PYCAT_VERSION)),
    }
    normalized_token = str(token or "").strip()
    if normalized_token:
        headers.update(
            {
                "Authorization": f"Bearer {normalized_token}",
            }
        )
    return headers


def random_wechat_uin() -> str:
    value = str(secrets.randbits(32))
    return base64.b64encode(value.encode("ascii")).decode("ascii")


class WeChatChannelClient:
    """HTTP client for official WeChat and the experimental iLink transport."""

    def __init__(self) -> None:
        self._access_token_cache: dict[str, tuple[str, float]] = {}

    def create_ilink_login(self, *, local_tokens: tuple[str, ...] = ()) -> dict[str, Any]:
        payload = {"local_token_list": [str(token).strip() for token in local_tokens[:10] if str(token).strip()]}
        with httpx.Client(timeout=WECHAT_QR_CREATE_TIMEOUT, follow_redirects=True) as client:
            response = client.post(
                f"{WECHAT_ILINK_LOGIN_BASE}/ilink/bot/get_bot_qrcode",
                params={"bot_type": "3"},
                json=payload,
                headers=ilink_headers(),
            )
            response.raise_for_status()
            return self.read_json(response, operation="create WeChat login")

    def poll_ilink_login(
        self,
        *,
        qrcode: str,
        api_base: str = WECHAT_ILINK_LOGIN_BASE,
        verification_code: str = "",
    ) -> dict[str, Any]:
        params = {"qrcode": str(qrcode or "").strip()}
        if not params["qrcode"]:
            raise ValueError("微信登录状态查询缺少二维码标识。")
        code = str(verification_code or "").strip()
        if code:
            params["verify_code"] = code
        try:
            with httpx.Client(timeout=40.0, follow_redirects=True) as client:
                response = client.get(
                    f"{normalize_api_base(api_base)}/ilink/bot/get_qrcode_status",
                    params=params,
                    headers=ilink_headers(),
                )
                response.raise_for_status()
                return self.read_json(response, operation="poll WeChat login")
        except httpx.TimeoutException:
            return {"status": "wait"}

    def fetch_ilink_updates(
        self,
        *,
        api_base: str,
        token: str,
        cursor: str,
    ) -> dict[str, Any]:
        with httpx.Client(timeout=40.0, follow_redirects=True) as client:
            response = client.post(
                f"{normalize_api_base(api_base)}/ilink/bot/getupdates",
                json={
                    "get_updates_buf": str(cursor or ""),
                    "base_info": self.base_info(),
                },
                headers=ilink_headers(token=token),
            )
            response.raise_for_status()
            return self.read_json(response, operation="fetch WeChat updates")

    def notify_ilink_start(self, *, api_base: str, token: str) -> None:
        self._notify_ilink(api_base=api_base, token=token, operation="notifystart")

    def notify_ilink_stop(self, *, api_base: str, token: str) -> None:
        self._notify_ilink(api_base=api_base, token=token, operation="notifystop")

    def _notify_ilink(self, *, api_base: str, token: str, operation: str) -> None:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            response = client.post(
                f"{normalize_api_base(api_base)}/ilink/bot/msg/{operation}",
                json={"base_info": self.base_info()},
                headers=ilink_headers(token=token),
            )
            response.raise_for_status()
            self.read_json(response, operation=f"WeChat {operation}")

    def send_ilink_reply(
        self,
        channel: ChannelConfig,
        *,
        touser: str,
        content: str,
        context_token: str = "",
    ) -> None:
        self.send_ilink_items(channel, touser=touser, context_token=context_token, items=[
            {'type': WECHAT_ITEM_TEXT, 'text_item': {'text': normalize_wechat_reply_text(content)}},
        ])

    def send_ilink_items(self, channel: ChannelConfig, *, touser: str, items: list[dict], context_token: str = '') -> None:
        credentials = self.resolve_ilink_credentials(channel)
        if credentials is None:
            raise RuntimeError("个人微信扫码连接尚未完成，无法回发消息。")
        if not str(touser or "").strip():
            raise RuntimeError("个人微信连接缺少目标用户，无法回发消息。")
        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": str(touser or "").strip(),
                "client_id": str(uuid.uuid4()),
                "message_type": WECHAT_MESSAGE_BOT,
                "message_state": WECHAT_MESSAGE_FINISH,
                "context_token": str(context_token or "").strip(),
                "item_list": items,
            },
            "base_info": self.base_info(),
        }
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            response = client.post(
                f"{credentials['api_base']}/ilink/bot/sendmessage",
                json=payload,
                headers=ilink_headers(token=credentials["token"]),
            )
            response.raise_for_status()
            self.read_json(response, operation="send WeChat reply")

    def send_official_reply(self, access_token: str, *, touser: str, content: str) -> None:
        url = f"https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token={access_token}"
        payload = {
            "touser": touser,
            "msgtype": "text",
            "text": {"content": normalize_wechat_reply_text(content)},
        }
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
        if int(data.get("errcode", 0) or 0) != 0:
            raise RuntimeError(f"微信客服接口返回错误: {data.get('errmsg') or data}")

    def get_access_token(self, channel: ChannelConfig, *, app_id: str, app_secret: str) -> str:
        cache_key = str(channel.id or "").strip() or f"wechat::{app_id}"
        cached = self._access_token_cache.get(cache_key)
        current = time.time()
        if cached and cached[0] and cached[1] > current + 60:
            return cached[0]
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            response = client.get(
                "https://api.weixin.qq.com/cgi-bin/token",
                params={
                    "grant_type": "client_credential",
                    "appid": app_id,
                    "secret": app_secret,
                },
            )
            response.raise_for_status()
            data = response.json()
        token = str(data.get("access_token", "") or "").strip()
        if not token:
            raise RuntimeError(f"获取微信 access_token 失败: {data.get('errmsg') or data}")
        expires_in = int(data.get("expires_in", 7200) or 7200)
        self._access_token_cache[cache_key] = (token, current + max(300, expires_in))
        return token

    @staticmethod
    def resolve_ilink_credentials(channel: ChannelConfig) -> dict[str, str] | None:
        config = dict(getattr(channel, "config", {}) or {})
        token = str(config.get("ilink_token", "") or "").strip()
        api_base = str(config.get("ilink_api_base", "") or "").strip()
        bot_id = str(config.get("ilink_bot_id", "") or "").strip()
        if not (token and api_base and bot_id):
            return None
        return {
            "token": token,
            "api_base": normalize_api_base(api_base),
            "bot_id": bot_id,
            "user_id": str(config.get("ilink_user_id", "") or "").strip(),
        }

    @staticmethod
    def base_info() -> dict[str, str]:
        return {
            "channel_version": PYCAT_VERSION,
            "bot_agent": PYCAT_BOT_AGENT,
        }

    @staticmethod
    def read_json(response: httpx.Response, *, operation: str) -> dict[str, Any]:
        raw = response.json()
        if not isinstance(raw, dict):
            return {}
        ret = _int_value(raw.get("ret"))
        errcode = _int_value(raw.get("errcode"))
        code = ret if ret != 0 else errcode
        if code != 0:
            raise WeChatAPIError(str(raw.get("errmsg") or f"{operation} failed"), code=code)
        return raw

    @staticmethod
    def is_reauth_required(exc: Exception) -> bool:
        if isinstance(exc, WeChatAPIError) and exc.code in {-14, 401, 403}:
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            return int(getattr(exc.response, "status_code", 0) or 0) in {401, 403}
        text = str(exc or "").strip().lower()
        return any(item in text for item in ("session expired", "login expired", "invalid token"))

    @staticmethod
    def coalesce_text(mapping: Mapping[str, Any], *keys: str, default: str = "") -> str:
        for key in keys:
            text = str(mapping.get(key, "") or "").strip()
            if text:
                return text
        return str(default or "").strip()


def normalize_api_base(value: Any) -> str:
    normalized = str(value or "").strip().rstrip("/")
    if not normalized:
        raise ValueError("微信服务端未返回消息 API 地址。")
    if normalized.startswith("http://") or normalized.startswith("https://"):
        return normalized
    return f"https://{normalized}"


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "PYCAT_BOT_AGENT",
    "PYCAT_VERSION",
    "WECHAT_ILINK_APP_ID",
    "WECHAT_ILINK_LOGIN_BASE",
    "WECHAT_QR_CREATE_TIMEOUT",
    "WeChatAPIError",
    "WeChatChannelClient",
    "encode_client_version",
    "ilink_headers",
    "normalize_api_base",
]
