from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class TelegramUpdateEnvelope:
    kind: str
    content: str = ""
    user_id: str = ""
    chat_id: str = ""
    message_id: str = ""
    thread_id: str = ""
    reply_user: str = ""
    update_id: str = ""
    event_type: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def normalize_telegram_update(
    payload: Any,
    *,
    mark_recent: Callable[[str], bool] | None = None,
) -> TelegramUpdateEnvelope | None:
    if not isinstance(payload, dict):
        raise ValueError("invalid telegram update")

    update_id = _coalesce_text(payload, "update_id")
    callback_query = payload.get("callback_query") if isinstance(payload.get("callback_query"), dict) else None
    if callback_query is not None:
        return _normalize_callback_query(payload, callback_query, update_id=update_id, mark_recent=mark_recent)

    event_type, message = _resolve_message_payload(payload)
    if not event_type or not isinstance(message, dict):
        return None

    from_user = message.get("from") if isinstance(message.get("from"), dict) else {}
    if bool(from_user.get("is_bot", False)):
        return None

    content = extract_telegram_message_text(message)
    if not content:
        return None

    return _build_message_envelope(
        message,
        content=content,
        update_id=update_id,
        event_type=event_type,
        mark_recent=mark_recent,
    )


def extract_telegram_message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    text = _coalesce_text(message, "text")
    if text:
        return text
    caption = _coalesce_text(message, "caption")
    if caption:
        return caption
    if isinstance(message.get("photo"), list) and message.get("photo"):
        return "[图片]"
    document = message.get("document") if isinstance(message.get("document"), dict) else None
    if document is not None:
        file_name = _coalesce_text(document, "file_name")
        return f"[文件] {file_name}".strip()
    if isinstance(message.get("voice"), dict):
        return "[语音]"
    if isinstance(message.get("audio"), dict):
        return "[音频]"
    if isinstance(message.get("video"), dict):
        return "[视频]"
    if isinstance(message.get("animation"), dict):
        return "[动图]"
    sticker = message.get("sticker") if isinstance(message.get("sticker"), dict) else None
    if sticker is not None:
        emoji = _coalesce_text(sticker, "emoji")
        return f"[贴纸] {emoji}".strip()
    if isinstance(message.get("location"), dict):
        return "[位置]"
    if isinstance(message.get("contact"), dict):
        return "[联系人]"
    return ""


def _normalize_callback_query(
    payload: dict[str, Any],
    callback_query: dict[str, Any],
    *,
    update_id: str,
    mark_recent: Callable[[str], bool] | None,
) -> TelegramUpdateEnvelope | None:
    from_user = callback_query.get("from") if isinstance(callback_query.get("from"), dict) else {}
    if bool(from_user.get("is_bot", False)):
        return None

    message = callback_query.get("message") if isinstance(callback_query.get("message"), dict) else {}
    data = _coalesce_text(callback_query, "data")
    content = data or extract_telegram_message_text(message)
    if not content:
        return None

    callback_id = _coalesce_text(callback_query, "id")
    envelope = _build_message_envelope(
        message,
        content=content,
        update_id=update_id,
        event_type="callback_query",
        mark_recent=mark_recent,
        fallback_user=from_user,
        fallback_message_id=callback_id,
        extra_meta={"callback_query_id": callback_id, "callback_data": data},
    )
    if envelope is not None:
        return envelope


    user_id = _coalesce_text(from_user, "id", "username")
    dedupe_key = callback_id or update_id
    if dedupe_key and callable(mark_recent) and not bool(mark_recent(dedupe_key)):
        return None
    meta = {
        "user": user_id,
        "reply_user": user_id,
        "thread_id": user_id,
        "chat_id": user_id,
        "message_id": callback_id,
        "context_token": callback_id,
        "platform": "telegram",
        "event_type": "callback_query",
        "update_id": update_id,
        "callback_query_id": callback_id,
        "callback_data": data,
    }
    return TelegramUpdateEnvelope(
        kind="message",
        content=content,
        user_id=user_id,
        chat_id=user_id,
        message_id=callback_id,
        thread_id=user_id,
        reply_user=user_id,
        update_id=update_id,
        event_type="callback_query",
        meta=meta,
    )


def _build_message_envelope(
    message: dict[str, Any],
    *,
    content: str,
    update_id: str,
    event_type: str,
    mark_recent: Callable[[str], bool] | None,
    fallback_user: dict[str, Any] | None = None,
    fallback_message_id: str = "",
    extra_meta: dict[str, Any] | None = None,
) -> TelegramUpdateEnvelope | None:
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    from_user = message.get("from") if isinstance(message.get("from"), dict) else (fallback_user or {})
    sender_chat = message.get("sender_chat") if isinstance(message.get("sender_chat"), dict) else {}

    chat_id = _coalesce_text(chat, "id")
    message_id = _coalesce_text(message, "message_id") or str(fallback_message_id or "").strip()
    message_thread_id = _coalesce_text(message, "message_thread_id")
    user_id = (
        _coalesce_text(from_user, "id", "username")
        or _coalesce_text(sender_chat, "id", "username", "title")
        or chat_id
    )
    thread_id = f"{chat_id}:{message_thread_id}" if chat_id and message_thread_id else (chat_id or user_id)
    reply_user = chat_id or user_id
    chat_type = _coalesce_text(chat, "type")

    dedupe_key = f"{chat_id}:{message_id}" if chat_id and message_id else (message_id or update_id)
    if dedupe_key and callable(mark_recent) and not bool(mark_recent(dedupe_key)):
        return None

    meta = {
        "user": user_id,
        "reply_user": reply_user,
        "thread_id": thread_id,
        "chat_id": chat_id,
        "message_id": message_id,
        "context_token": message_id,
        "platform": "telegram",
        "chat_type": chat_type,
        "event_type": event_type,
        "update_id": update_id,
        "message_thread_id": message_thread_id,
    }
    for key, value in dict(extra_meta or {}).items():
        meta[key] = value
    return TelegramUpdateEnvelope(
        kind="message",
        content=content,
        user_id=user_id,
        chat_id=chat_id,
        message_id=message_id,
        thread_id=thread_id,
        reply_user=reply_user,
        update_id=update_id,
        event_type=event_type,
        meta=meta,
    )


def _resolve_message_payload(payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    for key in (
        "message",
        "edited_message",
        "channel_post",
        "edited_channel_post",
        "business_message",
        "edited_business_message",
    ):
        value = payload.get(key)
        if isinstance(value, dict):
            return key, value
    return "", None


def _coalesce_text(mapping: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        text = str(value if value is not None else "").strip()
        if text:
            return text
    return ""


__all__ = ["TelegramUpdateEnvelope", "extract_telegram_message_text", "normalize_telegram_update"]