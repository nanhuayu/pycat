from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class QQBotWebhookEnvelope:
    kind: str
    challenge: str = ""
    content: str = ""
    user_id: str = ""
    thread_id: str = ""
    reply_user: str = ""
    message_id: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def normalize_qqbot_webhook_payload(
    payload: Any,
    *,
    mark_recent: Callable[[str], bool] | None = None,
) -> QQBotWebhookEnvelope | None:
    """Normalize QQ Bot webhook / bridge payloads into channel messages.

    The QQ ecosystem has several deployment shapes (official callbacks, bot
    gateway relays, self-hosted bridges). This normalizer intentionally accepts
    a small common envelope instead of hard-coding one vendor payload:
    ``content/text``, ``message.id``, ``author/user`` and target identifiers.
    """

    if not isinstance(payload, dict):
        raise ValueError("invalid qqbot payload")

    payload_type = _coalesce_text(payload, "type", "event_type", "t").lower()
    if payload_type in {"url_verification", "challenge"} or _coalesce_text(payload, "challenge"):
        return QQBotWebhookEnvelope(kind="challenge", challenge=_coalesce_text(payload, "challenge"))

    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    data = event.get("d") if isinstance(event.get("d"), dict) else event
    message = data.get("message") if isinstance(data.get("message"), dict) else data
    event_type = _coalesce_text(payload, "t", "type", "event_type") or _coalesce_text(event, "t", "type", "event_type")
    event_type = _normalize_event_type(event_type)

    author = message.get("author") if isinstance(message.get("author"), dict) else {}
    user = message.get("user") if isinstance(message.get("user"), dict) else {}
    member = message.get("member") if isinstance(message.get("member"), dict) else {}
    member_user = member.get("user") if isinstance(member.get("user"), dict) else {}

    if bool(author.get("bot", False)) or bool(user.get("bot", False)) or bool(member_user.get("bot", False)):
        return None

    content = extract_qqbot_message_text(message)
    if not content:
        return None

    payload_event_id = _coalesce_text(payload, "id", "event_id") or _coalesce_text(event, "id", "event_id")
    message_id = _coalesce_text(message, "message_id", "msg_id", "id", "event_id")
    if message_id and callable(mark_recent) and not bool(mark_recent(message_id)):
        return None

    target_type, target_id = _resolve_reply_target(event_type, message)
    user_id = (
        _coalesce_text(author, "member_openid", "user_openid", "id", "user_id", "openid", "open_id")
        or _coalesce_text(user, "id", "user_id", "openid", "open_id")
        or _coalesce_text(member_user, "id", "user_id", "openid", "open_id")
        or _coalesce_text(message, "user_openid", "member_openid", "user_id", "openid", "open_id", "author_id")
    )
    thread_id = _coalesce_text(
        message,
        "thread_id",
        "channel_id",
        "group_id",
        "group_openid",
        "chat_id",
        "guild_id",
        "target_id",
    ) or user_id
    reply_user = target_id or thread_id or user_id
    context_token = _coalesce_text(message, "context_token", "msg_id", "message_id", "id")

    meta = {
        "event_id": payload_event_id,
        "event_type": event_type,
        "user": user_id,
        "reply_user": reply_user,
        "target_type": target_type,
        "thread_id": thread_id,
        "chat_id": thread_id,
        "message_id": message_id,
        "platform": "qqbot",
        "context_token": context_token,
    }
    return QQBotWebhookEnvelope(
        kind="message",
        content=content,
        user_id=user_id,
        thread_id=thread_id,
        reply_user=reply_user,
        message_id=message_id,
        meta=meta,
    )


def extract_qqbot_message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    text = _coalesce_text(message, "content", "text", "message", "plain_text")
    if text:
        return text

    attachments = message.get("attachments")
    if isinstance(attachments, list) and attachments:
        return "\n".join(_attachment_label(item) for item in attachments if isinstance(item, dict)).strip()

    message_type = _coalesce_text(message, "message_type", "msg_type").lower()
    if message_type in {"image", "photo"}:
        return "[图片]"
    if message_type in {"file", "attachment"}:
        return "[文件]"
    if message_type in {"audio", "voice"}:
        return "[语音]"
    if message_type in {"video"}:
        return "[视频]"
    return ""


def _resolve_reply_target(event_type: str, message: dict[str, Any]) -> tuple[str, str]:
    normalized_event = str(event_type or "").strip().upper()
    explicit = _coalesce_text(message, "reply_user", "reply_to", "target_id")
    if explicit:
        return _coalesce_text(message, "target_type") or "channel", explicit
    if normalized_event in {"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE", "GROUP_MSG_RECEIVE", "GROUP_ADD_ROBOT"}:
        return "group", _coalesce_text(message, "group_openid", "group_id", "chat_id")
    if normalized_event in {"C2C_MESSAGE_CREATE", "C2C_MSG_RECEIVE", "FRIEND_ADD"}:
        author = message.get("author") if isinstance(message.get("author"), dict) else {}
        return "user", _coalesce_text(message, "user_openid", "openid", "open_id", "user_id", "author_id") or _coalesce_text(author, "user_openid", "openid", "open_id", "id")
    if normalized_event in {"DIRECT_MESSAGE_CREATE"}:
        return "dm", _coalesce_text(message, "guild_id", "src_guild_id", "channel_id", "chat_id")
    return "channel", _coalesce_text(message, "channel_id", "group_openid", "group_id", "chat_id", "guild_id")


def _normalize_event_type(value: str) -> str:
    normalized = str(value or "").strip().upper()
    aliases = {
        "MESSAGE_CREATE": "AT_MESSAGE_CREATE",
        "GROUP_MESSAGE": "GROUP_MESSAGE_CREATE",
        "GROUP_AT_MESSAGE": "GROUP_AT_MESSAGE_CREATE",
        "C2C_MESSAGE": "C2C_MESSAGE_CREATE",
        "DIRECT_MESSAGE": "DIRECT_MESSAGE_CREATE",
    }
    return aliases.get(normalized, normalized)


def _attachment_label(item: dict[str, Any]) -> str:
    filename = _coalesce_text(item, "filename", "file_name", "name")
    content_type = _coalesce_text(item, "content_type", "type").lower()
    if "image" in content_type:
        return f"[图片] {filename}".strip()
    if "audio" in content_type or "voice" in content_type:
        return f"[语音] {filename}".strip()
    if "video" in content_type:
        return f"[视频] {filename}".strip()
    return f"[文件] {filename}".strip()


def _coalesce_text(mapping: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        text = str(value or "").strip()
        if text:
            return text
    return ""


__all__ = ["QQBotWebhookEnvelope", "extract_qqbot_message_text", "normalize_qqbot_webhook_payload"]