"""Channel reply normalization and dispatch helpers."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from models.contracts.agent import RunStatus, RunStopReason
from models.contracts.channel import ChannelConfig
from models.conversation import Message

logger = logging.getLogger(__name__)


def normalize_reply_text(
    content: Any,
    *,
    normalizer: Callable[[str], str] | None = None,
    fallback_text: str = "",
) -> str:
    raw = str(content or "")
    if callable(normalizer):
        text = str(normalizer(raw) or "").strip()
        if text:
            return text
    fallback = raw.replace("\r\n", "\n").strip()
    return fallback or str(fallback_text or "")


def normalize_step_reply_text(content: Any, *, normalizer: Callable[[str], str] | None = None) -> str:
    raw = str(content or "").replace("\r\n", "\n").strip()
    if not raw:
        return ""
    if callable(normalizer):
        text = str(normalizer(raw) or "").strip()
        if text:
            return text
    return raw


def channel_reply_policy(channel: Any) -> str:
    config = dict(getattr(channel, "config", {}) or {})
    raw = (
        config.get("channel_reply_policy")
        or config.get("reply_policy")
        or config.get("outbound_reply_policy")
        or "assistant_messages"
    )
    normalized = str(raw or "assistant_messages").strip().lower().replace("-", "_")
    aliases = {
        "assistant": "assistant_messages",
        "assistant_message": "assistant_messages",
        "assistant_steps": "assistant_messages",
        "steps": "assistant_messages",
        "step": "assistant_messages",
        "final_message": "final",
        "final_only": "final",
        "none": "none",
        "off": "none",
        "silent": "none",
    }
    return aliases.get(normalized, normalized or "assistant_messages")


def channel_send_thinking(channel: Any) -> bool:
    config = dict(getattr(channel, "config", {}) or {})
    for key in ("send_thinking_to_channel", "send_thinking", "channel_send_thinking", "reply_thinking"):
        if key not in config:
            continue
        value = config.get(key)
        if isinstance(value, bool):
            return value
        normalized = str(value or "").strip().lower()
        return normalized in {"1", "true", "yes", "on", "enabled", "\u5f00\u542f", "\u662f"}
    return False


def mark_channel_owned(message: Message | None, *, channel_id: str) -> None:
    if message is None:
        return
    try:
        metadata = dict(getattr(message, "metadata", {}) or {})
        metadata["channel_owned"] = True
        metadata["channel_id"] = str(channel_id or "")
        message.metadata = metadata
    except Exception as exc:
        logger.debug("Failed to mark channel gateway owned message: %s", exc)


@dataclass
class ChannelReplyDispatcher:
    channel_id: str
    platform_label: str
    reply_sender: Callable[[str, Message | None], None] | None = None
    normalizer: Callable[[str], str] | None = None
    send_thinking: bool = False
    sent_count: int = 0
    sent_step_keys: set[str] = field(default_factory=set)
    sent_message_contents: dict[str, str] = field(default_factory=dict)

    @property
    def can_send(self) -> bool:
        return callable(self.reply_sender)

    def mark_owned(self, message: Message | None) -> None:
        mark_channel_owned(message, channel_id=self.channel_id)

    @staticmethod
    def message_key(message: Message | None) -> str:
        if message is None:
            return ""
        return (
            str(getattr(message, "id", "") or "").strip()
            or str(getattr(message, "seq_id", "") or "").strip()
            or f"object:{id(message)}"
        )

    def has_dispatched_content(self, message: Message | None, content: str) -> bool:
        key = self.message_key(message)
        return bool(key) and self.sent_message_contents.get(key) == str(content or "").strip()

    def dispatch(
        self,
        content: str,
        *,
        source_message: Message | None = None,
        track_message_content: bool = False,
    ) -> bool:
        if not self.can_send or not callable(self.reply_sender):
            return False
        text = str(content or "").strip()
        if not text:
            return False
        try:
            self.reply_sender(text, source_message)
        except Exception as exc:
            logger.warning("Failed to send %s channel reply for channel %s: %s", self.platform_label, self.channel_id, exc)
            return False
        self.sent_count += 1
        if track_message_content:
            key = self.message_key(source_message)
            if key:
                self.sent_message_contents[key] = text
        return True

    def dispatch_assistant_step(self, step_message: Message) -> None:
        if str(getattr(step_message, "role", "") or "").strip().lower() != "assistant":
            return
        self.mark_owned(step_message)
        metadata = getattr(step_message, "metadata", {}) or {}
        if isinstance(metadata, dict) and metadata.get("incomplete"):
            return
        step_key = self.message_key(step_message)
        if step_key in self.sent_step_keys:
            return
        self.sent_step_keys.add(step_key)
        if self.send_thinking:
            thinking_text = str(getattr(step_message, "thinking", "") or "").strip()
            if thinking_text:
                self.dispatch(
                    normalize_step_reply_text(f"<think>\n{thinking_text}\n</think>", normalizer=self.normalizer),
                    source_message=step_message,
                )
        content = normalize_step_reply_text(getattr(step_message, "content", ""), normalizer=self.normalizer)
        if content:
            self.dispatch(
                content,
                source_message=step_message,
                track_message_content=True,
            )


class ChannelReplyCoordinator:
    """Applies channel reply policy to Agent turn results."""

    def __init__(
        self,
        *,
        channel: ChannelConfig,
        platform_label: str,
        reply_normalizer: Callable[[str], str] | None = None,
        reply_sender: Callable[[str, Message | None], None] | None = None,
    ) -> None:
        reply_policy = channel_reply_policy(channel)
        can_send_replies = callable(reply_sender) and reply_policy not in {"none", "silent", "off"}
        self.send_step_replies = can_send_replies and reply_policy in {"assistant_messages", "assistant_steps", "steps", "all", ""}
        self.dispatcher = ChannelReplyDispatcher(
            channel_id=str(getattr(channel, "id", "") or ""),
            platform_label=platform_label,
            reply_sender=reply_sender if can_send_replies else None,
            normalizer=reply_normalizer,
            send_thinking=self.send_step_replies and channel_send_thinking(channel),
        )
        self._reply_normalizer = reply_normalizer

    @property
    def assistant_step_callback(self) -> Callable[[Message], None] | None:
        return self.dispatcher.dispatch_assistant_step if self.send_step_replies else None

    def completed_reply(self, final_message: Message | None) -> str:
        self.dispatcher.mark_owned(final_message)
        reply_text = normalize_reply_text(
            getattr(final_message, "content", "") if final_message is not None else "",
            normalizer=self._reply_normalizer,
            fallback_text="模型已结束运行，但未返回可发送的最终文本。",
        )
        if not self.dispatcher.has_dispatched_content(final_message, reply_text):
            self.dispatcher.dispatch(
                reply_text,
                source_message=final_message,
                track_message_content=True,
            )
        return reply_text

    def interrupted_reply(
        self,
        final_message: Message | None,
        *,
        stop_reason: RunStopReason | str | None = None,
        error: str = "",
    ) -> str:
        self.dispatcher.mark_owned(final_message)
        reason = getattr(stop_reason, "value", stop_reason)
        reason = str(reason or "").strip().lower()
        partial = normalize_step_reply_text(
            getattr(final_message, "content", "") if final_message is not None else "",
            normalizer=self._reply_normalizer,
        )
        if reason == RunStopReason.OUTPUT_LIMIT.value:
            notice = "模型在生成最终回复前达到输出上限，请调高模型最大输出或重试。"
            reply_text = f"{partial}\n\n{notice}" if partial else notice
        elif reason == RunStopReason.EXPLICIT_COMPLETION_MISSING.value:
            reply_text = "模型未通过完成协议提交最终回复，请重试。"
        elif reason == RunStopReason.EMPTY_RESPONSE.value:
            reply_text = "模型未返回可发送的文本回复，请重试。"
        elif reason == RunStopReason.INCOMPLETE_RESPONSE.value:
            notice = "模型响应未完整结束，请检查模型或服务商状态后重试。"
            reply_text = f"{partial}\n\n{notice}" if partial else notice
        else:
            reply_text = normalize_reply_text(
                error or "消息处理尚未完成，请重试。",
                normalizer=self._reply_normalizer,
                fallback_text="消息处理尚未完成，请重试。",
            )
        if not self.dispatcher.has_dispatched_content(final_message, reply_text):
            self.dispatcher.dispatch(
                reply_text,
                source_message=final_message,
                track_message_content=True,
            )
        return reply_text

    def cancelled_reply(self) -> str:
        reply_text = normalize_reply_text(
            "消息已收到，但处理过程被取消。",
            normalizer=self._reply_normalizer,
            fallback_text="消息已收到，但处理过程被取消。",
        )
        self.dispatcher.dispatch(reply_text, source_message=None)
        return reply_text

    def failed_reply(self, error: str = "") -> str:
        reply_text = normalize_reply_text(
            error or "消息已收到，但生成回复时失败。",
            normalizer=self._reply_normalizer,
            fallback_text="消息已收到，但生成回复时失败。",
        )
        self.dispatcher.dispatch(reply_text, source_message=None)
        return reply_text

    @staticmethod
    def is_completed(status: Any) -> bool:
        return getattr(status, "value", status) == RunStatus.COMPLETED.value

    @staticmethod
    def is_cancelled(status: Any) -> bool:
        return getattr(status, "value", status) == RunStatus.CANCELLED.value

    @staticmethod
    def is_interrupted(status: Any) -> bool:
        return getattr(status, "value", status) == RunStatus.INTERRUPTED.value
