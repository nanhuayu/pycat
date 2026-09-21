from __future__ import annotations

import hashlib
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass


def verify_wechat_signature(
    token: str,
    signature: str,
    timestamp: str,
    nonce: str,
) -> bool:
    token_text = str(token or "").strip()
    signature_text = str(signature or "").strip()
    timestamp_text = str(timestamp or "").strip()
    nonce_text = str(nonce or "").strip()
    if not (token_text and signature_text and timestamp_text and nonce_text):
        return False
    pieces = sorted([token_text, timestamp_text, nonce_text])
    digest = hashlib.sha1("".join(pieces).encode("utf-8")).hexdigest()
    return digest == signature_text


@dataclass(frozen=True)
class WeChatInboundMessage:
    to_user: str
    from_user: str
    msg_type: str
    content: str = ""
    message_id: str = ""
    event: str = ""
    event_key: str = ""
    create_time: int = 0
    raw_xml: str = ""
    encrypted: bool = False

    @property
    def dedupe_key(self) -> str:
        if self.message_id:
            return self.message_id
        base = f"{self.from_user}:{self.create_time}:{self.msg_type}:{self.event}:{self.content}"
        return hashlib.sha1(base.encode("utf-8")).hexdigest()

    @property
    def is_text(self) -> bool:
        return self.msg_type == "text" and bool((self.content or "").strip())


def parse_wechat_message(xml_text: str) -> WeChatInboundMessage:
    payload = str(xml_text or "").strip()
    if not payload:
        raise ValueError("empty wechat payload")

    root = ET.fromstring(payload)

    def _text(name: str) -> str:
        node = root.find(name)
        if node is None or node.text is None:
            return ""
        return str(node.text or "").strip()

    create_time = 0
    try:
        create_time = int(_text("CreateTime") or 0)
    except Exception:
        create_time = 0

    return WeChatInboundMessage(
        to_user=_text("ToUserName"),
        from_user=_text("FromUserName"),
        msg_type=_text("MsgType").lower(),
        content=_text("Content"),
        message_id=_text("MsgId"),
        event=_text("Event").lower(),
        event_key=_text("EventKey"),
        create_time=create_time,
        raw_xml=payload,
        encrypted=bool(_text("Encrypt")),
    )


def build_wechat_text_reply(*, to_user: str, from_user: str, content: str) -> str:
    reply_text = normalize_wechat_reply_text(content)
    now = int(time.time())
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{to_user or ''}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user or ''}]]></FromUserName>"
        f"<CreateTime>{now}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{reply_text}]]></Content>"
        "</xml>"
    )


def normalize_wechat_reply_text(content: str, *, limit: int = 1800) -> str:
    text = str(content or "").replace("\r\n", "\n").strip()
    if not text:
        return "已收到消息，但暂时没有可发送的文本回复。"
    text = text.replace("]]>", "] ]>")
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


__all__ = [
    "WeChatInboundMessage",
    "build_wechat_text_reply",
    "normalize_wechat_reply_text",
    "parse_wechat_message",
    "verify_wechat_signature",
]