from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Mapping

from pycat.core.channel.connection import (
    ChannelConnectionSnapshot,
    ChannelConnectionState,
    ChannelRequiredAction,
)
from pycat.core.channel.platforms.wechat.client import (
    WECHAT_ILINK_LOGIN_BASE,
    WeChatChannelClient,
    normalize_api_base,
)
from pycat.models.contracts.channel import ChannelConfig

LOGIN_TTL_SECONDS = 5 * 60


@dataclass(frozen=True)
class WeChatLoginSession:
    id: str
    channel: ChannelConfig
    qrcode: str
    qr_text: str
    started_at: float
    api_base: str
    snapshot: ChannelConnectionSnapshot
    pending_verification_code: str = ""
    already_connected: bool = False

    @property
    def is_complete(self) -> bool:
        return self.snapshot.is_ready


class WeChatLoginFlow:
    """One explicit iLink QR login state machine.

    Polling advances the existing session and never creates a replacement QR.
    A new QR is created only by calling :meth:`start` again.
    """

    def __init__(self, client: WeChatChannelClient | None = None) -> None:
        self._client = client or WeChatChannelClient()

    def start(
        self,
        channel: ChannelConfig,
        *,
        local_tokens: tuple[str, ...] = (),
    ) -> WeChatLoginSession:
        config = dict(channel.config or {})
        config["connection_mode"] = "ilink"
        normalized_channel = replace(channel, enabled=True, config=config)
        payload = _payload_fields(self._client.create_ilink_login(local_tokens=local_tokens))
        qrcode = self._client.coalesce_text(payload, "qrcode")
        qr_text = self._client.coalesce_text(payload, "qrcode_img_content")
        if not (qrcode and qr_text):
            raise RuntimeError("微信服务端没有返回有效二维码。")
        snapshot = ChannelConnectionSnapshot(
            channel_id=normalized_channel.id,
            channel_type="wechat",
            mode="ilink",
            state=ChannelConnectionState.WAITING_USER,
            required_action=ChannelRequiredAction.SCAN,
            detail="二维码已生成，请使用手机微信扫码。",
            qr_text=qr_text,
            session_id=qrcode,
        )
        return WeChatLoginSession(
            id=str(uuid.uuid4()),
            channel=normalized_channel,
            qrcode=qrcode,
            qr_text=qr_text,
            started_at=time.monotonic(),
            api_base=WECHAT_ILINK_LOGIN_BASE,
            snapshot=snapshot,
        )

    def poll(
        self,
        session: WeChatLoginSession,
        *,
        verification_code: str = "",
    ) -> WeChatLoginSession:
        if time.monotonic() - session.started_at > LOGIN_TTL_SECONDS:
            return self._with_snapshot(
                session,
                state=ChannelConnectionState.ERROR,
                action=ChannelRequiredAction.RETRY,
                detail="二维码已过期，请重新生成。",
            )
        submitted_code = str(verification_code or session.pending_verification_code or "").strip()
        try:
            payload = _payload_fields(
                self._client.poll_ilink_login(
                    qrcode=session.qrcode,
                    api_base=session.api_base,
                    verification_code=submitted_code,
                )
            )
        except Exception as exc:
            return self._with_snapshot(
                session,
                state=ChannelConnectionState.RECONNECTING,
                action=ChannelRequiredAction.RETRY,
                detail=f"微信登录状态检查失败：{exc}",
            )

        status = self._client.coalesce_text(payload, "status", default="wait").lower()
        if status == "wait":
            return self._with_snapshot(
                session,
                state=ChannelConnectionState.WAITING_USER,
                action=ChannelRequiredAction.SCAN,
                detail="等待手机微信扫码。",
            )
        if status == "scaned":
            return replace(
                self._with_snapshot(
                    session,
                    state=ChannelConnectionState.WAITING_USER,
                    action=ChannelRequiredAction.NONE,
                    detail="二维码已扫描，请在手机上确认连接。",
                ),
                pending_verification_code="",
            )
        if status == "need_verifycode":
            detail = "验证码不正确，请重新输入。" if submitted_code else "请输入手机微信显示的数字验证码。"
            return replace(
                self._with_snapshot(
                    session,
                    state=ChannelConnectionState.WAITING_USER,
                    action=ChannelRequiredAction.VERIFY_CODE,
                    detail=detail,
                ),
                pending_verification_code=submitted_code,
            )
        if status == "verify_code_blocked":
            return replace(
                self._with_snapshot(
                    session,
                    state=ChannelConnectionState.ERROR,
                    action=ChannelRequiredAction.RETRY,
                    detail="验证码多次错误，本次二维码已被阻止，请重新生成。",
                ),
                pending_verification_code="",
            )
        if status == "expired":
            return self._with_snapshot(
                session,
                state=ChannelConnectionState.ERROR,
                action=ChannelRequiredAction.RETRY,
                detail="二维码已过期，请重新生成。",
            )
        if status == "scaned_but_redirect":
            redirect_host = self._client.coalesce_text(payload, "redirect_host")
            api_base = normalize_api_base(redirect_host) if redirect_host else session.api_base
            return replace(
                self._with_snapshot(
                    session,
                    state=ChannelConnectionState.CONNECTING,
                    action=ChannelRequiredAction.NONE,
                    detail="扫码已确认，正在切换微信服务节点。",
                ),
                api_base=api_base,
                pending_verification_code="",
            )
        if status == "binded_redirect":
            credentials = self._client.resolve_ilink_credentials(session.channel)
            if credentials is None:
                return replace(
                    self._with_snapshot(
                        session,
                        state=ChannelConnectionState.ERROR,
                        action=ChannelRequiredAction.RETRY,
                        detail="该微信账号已绑定，但当前连接没有可复用凭据，请删除后重新添加。",
                    ),
                    already_connected=True,
                )
            return replace(
                self._ready_session(
                    session,
                    token=credentials["token"],
                    bot_id=credentials["bot_id"],
                    user_id=credentials["user_id"],
                    api_base=credentials["api_base"],
                    detail="该微信账号已经连接。",
                ),
                already_connected=True,
            )
        if status == "confirmed":
            token = self._client.coalesce_text(payload, "bot_token")
            bot_id = self._client.coalesce_text(payload, "ilink_bot_id")
            user_id = self._client.coalesce_text(payload, "ilink_user_id")
            base_url = self._client.coalesce_text(payload, "baseurl", default=session.api_base)
            if not (token and bot_id and base_url):
                return self._with_snapshot(
                    session,
                    state=ChannelConnectionState.ERROR,
                    action=ChannelRequiredAction.RETRY,
                    detail="微信确认成功，但服务端返回的连接凭据不完整。",
                )
            return self._ready_session(
                session,
                token=token,
                bot_id=bot_id,
                user_id=user_id,
                api_base=normalize_api_base(base_url),
                detail="个人微信连接成功。",
            )
        return self._with_snapshot(
            session,
            state=ChannelConnectionState.RECONNECTING,
            action=ChannelRequiredAction.RETRY,
            detail=f"微信返回了未知登录状态：{status}",
        )

    def _ready_session(
        self,
        session: WeChatLoginSession,
        *,
        token: str,
        bot_id: str,
        user_id: str,
        api_base: str,
        detail: str,
    ) -> WeChatLoginSession:
        config = dict(session.channel.config or {})
        config.update(
            {
                "connection_mode": "ilink",
                "ilink_token": str(token or "").strip(),
                "ilink_bot_id": str(bot_id or "").strip(),
                "ilink_user_id": str(user_id or "").strip(),
                "ilink_api_base": normalize_api_base(api_base),
            }
        )
        channel = replace(session.channel, enabled=True, config=config)
        snapshot = ChannelConnectionSnapshot(
            channel_id=channel.id,
            channel_type="wechat",
            mode="ilink",
            state=ChannelConnectionState.READY,
            detail=detail,
            account_name=str(user_id or bot_id or "").strip(),
            raw={"bot_id": str(bot_id or "").strip(), "api_base": normalize_api_base(api_base)},
        )
        return replace(
            session,
            channel=channel,
            api_base=normalize_api_base(api_base),
            snapshot=snapshot,
            pending_verification_code="",
        )

    @staticmethod
    def _with_snapshot(
        session: WeChatLoginSession,
        *,
        state: ChannelConnectionState,
        action: ChannelRequiredAction,
        detail: str,
    ) -> WeChatLoginSession:
        snapshot = replace(
            session.snapshot,
            state=state,
            required_action=action,
            detail=detail,
            qr_text=session.qr_text,
            session_id=session.qrcode,
        )
        return replace(session, snapshot=snapshot)


def _payload_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    fields = dict(payload or {})
    for key in ("data", "result", "session"):
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            fields.update(dict(nested))
    return fields


__all__ = ["LOGIN_TTL_SECONDS", "WeChatLoginFlow", "WeChatLoginSession"]
