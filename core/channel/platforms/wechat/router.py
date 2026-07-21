from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


WECHAT_ILINK_MESSAGE_USER = 1
WECHAT_ILINK_ITEM_TEXT = 1
WECHAT_ILINK_ITEM_IMAGE = 2
WECHAT_ILINK_ITEM_VOICE = 3
WECHAT_ILINK_ITEM_FILE = 4
WECHAT_ILINK_ITEM_VIDEO = 5


@dataclass(frozen=True)
class WeChatILinkInboundEnvelope:
    user_id: str
    message_id: str
    content: str
    context_token: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def normalize_wechat_ilink_message(raw_message: Any, *, mark_recent) -> WeChatILinkInboundEnvelope | None:
    if not isinstance(raw_message, dict):
        return None

    try:
        message_type = int(raw_message.get("message_type", 0) or 0)
    except Exception:
        message_type = 0
    if message_type != WECHAT_ILINK_MESSAGE_USER:
        return None

    user_id = _coalesce_text(raw_message, "from_user_id", "user_id")
    if not user_id:
        return None

    message_id = _coalesce_text(raw_message, "message_id", "client_id")
    if message_id and not bool(mark_recent(message_id)):
        return None

    content = extract_wechat_ilink_text(raw_message.get("item_list"))
    if not content:
        return None

    context_token = _coalesce_text(raw_message, "context_token")
    meta = {
        "user": user_id,
        "reply_user": user_id,
        "thread_id": user_id,
        "chat_id": user_id,
        "message_id": message_id,
        "platform": "wechat",
        "context_token": context_token,
    }
    return WeChatILinkInboundEnvelope(
        user_id=user_id,
        message_id=message_id,
        content=content,
        context_token=context_token,
        meta=meta,
    )


def extract_wechat_ilink_text(item_list: Any) -> str:
    if not isinstance(item_list, list):
        return ""

    parts: list[str] = []
    for item in item_list:
        if not isinstance(item, dict):
            continue
        try:
            item_type = int(item.get("type", 0) or 0)
        except Exception:
            item_type = 0

        if item_type == WECHAT_ILINK_ITEM_TEXT:
            text_item = item.get("text_item")
            if isinstance(text_item, dict):
                text = str(text_item.get("text", "") or "").strip()
                if text:
                    parts.append(text)
        elif item_type == WECHAT_ILINK_ITEM_VOICE:
            voice_item = item.get("voice_item")
            text = str((voice_item or {}).get("text", "") if isinstance(voice_item, dict) else "").strip()
            parts.append(text or "[语音]")
        elif item_type == WECHAT_ILINK_ITEM_IMAGE:
            parts.append("[图片]")
        elif item_type == WECHAT_ILINK_ITEM_FILE:
            file_item = item.get("file_item")
            file_name = str((file_item or {}).get("file_name", "") if isinstance(file_item, dict) else "").strip()
            parts.append(f"[文件] {file_name}" if file_name else "[文件]")
        elif item_type == WECHAT_ILINK_ITEM_VIDEO:
            parts.append("[视频]")

    return "\n".join(part for part in parts if part).strip()


def _coalesce_text(mapping: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        text = str(value or "").strip()
        if text:
            return text
    return ""


__all__ = [
    "WeChatILinkInboundEnvelope",
    "extract_wechat_ilink_text",
    "normalize_wechat_ilink_message",
]
