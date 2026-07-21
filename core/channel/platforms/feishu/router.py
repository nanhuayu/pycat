from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable


_FEISHU_MENTION_RE = re.compile(r"@_user_\d+")


@dataclass(frozen=True)
class FeishuWebhookEnvelope:
    kind: str
    challenge: str = ""
    content: str = ""
    user_id: str = ""
    chat_id: str = ""
    message_id: str = ""
    chat_type: str = ""
    event_type: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def normalize_feishu_webhook_payload(
    payload: Any,
    *,
    expected_token: str = "",
    mark_recent: Callable[[str], bool] | None = None,
) -> FeishuWebhookEnvelope | None:
    if not isinstance(payload, dict):
        raise ValueError("invalid feishu payload")

    payload_type = str(payload.get("type", "") or "").strip().lower()
    if payload_type == "url_verification":
        token = str(payload.get("token", "") or "").strip()
        _validate_token(expected_token, token)
        return FeishuWebhookEnvelope(
            kind="challenge",
            challenge=str(payload.get("challenge", "") or "").strip(),
        )

    if "encrypt" in payload:
        raise ValueError("encrypted feishu callback is not supported yet")

    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    event_type = str(header.get("event_type", payload_type) or "").strip().lower()
    if event_type != "im.message.receive_v1":
        return None

    token = str(header.get("token", payload.get("token", "")) or "").strip()
    _validate_token(expected_token, token)

    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    sender_type = str(sender.get("sender_type", "") or "").strip().lower()
    if sender_type == "app":
        return None

    sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
    user_id = (
        str(sender_id.get("open_id", "") or "").strip()
        or str(sender_id.get("user_id", "") or "").strip()
        or str(sender_id.get("union_id", "") or "").strip()
    )

    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    message_id = str(message.get("message_id", "") or "").strip()
    if message_id and callable(mark_recent) and not bool(mark_recent(message_id)):
        return None

    chat_id = str(message.get("chat_id", "") or "").strip()
    chat_type = str(message.get("chat_type", "") or "").strip().lower()
    message_type = str(message.get("message_type", "") or "").strip().lower()
    content = extract_feishu_message_text(message_type, message.get("content"))
    if not content:
        return None

    meta = {
        "user": user_id,
        "reply_user": chat_id or user_id,
        "thread_id": chat_id or user_id,
        "chat_id": chat_id,
        "message_id": message_id,
        "platform": "feishu",
        "chat_type": chat_type,
        "event_type": event_type,
    }
    return FeishuWebhookEnvelope(
        kind="message",
        content=content,
        user_id=user_id,
        chat_id=chat_id,
        message_id=message_id,
        chat_type=chat_type,
        event_type=event_type,
        meta=meta,
    )


def extract_feishu_message_text(message_type: str, raw_content: Any) -> str:
    parsed = _parse_content(raw_content)
    kind = str(message_type or "").strip().lower()
    if kind == "text":
        text = str(parsed.get("text", "") or "").strip()
        text = _FEISHU_MENTION_RE.sub("", text).strip()
        return text
    if kind == "image":
        return "[图片]"
    if kind == "file":
        file_name = str(parsed.get("file_name", "") or "").strip()
        return f"[文件] {file_name}" if file_name else "[文件]"
    if kind == "audio":
        return "[音频]"
    if kind == "media":
        return "[多媒体]"
    if kind == "post":
        return _flatten_post_text(parsed)
    return ""


def _parse_content(raw_content: Any) -> dict[str, Any]:
    if isinstance(raw_content, dict):
        return dict(raw_content)
    if isinstance(raw_content, str):
        text = raw_content.strip()
        if not text:
            return {}
        try:
            value = json.loads(text)
        except Exception:
            return {"text": text}
        return value if isinstance(value, dict) else {}
    return {}


def _flatten_post_text(parsed: dict[str, Any]) -> str:
    content = parsed.get("zh_cn") if isinstance(parsed.get("zh_cn"), dict) else parsed
    rows = content.get("content") if isinstance(content, dict) else None
    if not isinstance(rows, list):
        return ""

    parts: list[str] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        row_parts: list[str] = []
        for item in row:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "") or "").strip()
            if text:
                row_parts.append(text)
        if row_parts:
            parts.append("".join(row_parts))
    return "\n".join(parts).strip()


def _validate_token(expected_token: str, actual_token: str) -> None:
    required = str(expected_token or "").strip()
    if not required:
        return
    if str(actual_token or "").strip() != required:
        raise ValueError("invalid feishu verification token")


__all__ = ["FeishuWebhookEnvelope", "extract_feishu_message_text", "normalize_feishu_webhook_payload"]