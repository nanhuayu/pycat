from __future__ import annotations

import logging
import uuid
from typing import Any, Callable

from pycat.core.channel.events import ChannelEvent
from pycat.core.channel.replies import ChannelReplyCoordinator
from pycat.core.channel.session_resolver import ChannelSessionResolver
from pycat.core.channel.turn_runner import ChannelTurnRunner
from pycat.models.contracts.channel import ChannelConfig
from pycat.models.conversation import Conversation, Message


logger = logging.getLogger(__name__)


class ChannelMessageProcessor:
    """Processes one normalized inbound message through a bound Agent turn."""

    def __init__(
        self,
        *,
        conv_service: Any,
        session_resolver: ChannelSessionResolver,
        turn_runner: ChannelTurnRunner,
        emit_event: Callable[[ChannelEvent], None],
        content_service=None,
    ) -> None:
        self._conv_service = conv_service
        self._session_resolver = session_resolver
        self._turn_runner = turn_runner
        self._emit_event = emit_event
        self._content_service = content_service

    def process_bound_message(
        self,
        channel: ChannelConfig,
        message: Message,
        *,
        binding_key: str,
        user_id: str,
        thread_id: str,
        reply_user: str,
        context_token: str,
        platform_label: str,
        reply_normalizer: Callable[[str], str] | None = None,
        binding_updates: dict[str, Any] | None = None,
        reply_sender: Callable[[str, Message | None], None] | None = None,
        attachments=(),
    ) -> tuple[Conversation, str] | None:
        inbound_text = str(getattr(message, "content", "") or "").strip()
        resolved_key = str(binding_key or thread_id or reply_user or user_id or "").strip()
        if not (resolved_key and reply_user and inbound_text):
            return None

        conversation, focus_requested = self._session_resolver.resolve_conversation(
            channel,
            binding_key=resolved_key,
            user_id=user_id,
        )
        conversation_id = str(getattr(conversation, "id", "") or "").strip()
        begin_turn = getattr(self._conv_service, "begin_turn", None)
        claim_token = begin_turn(conversation_id) if callable(begin_turn) else None
        if callable(begin_turn) and not claim_token:
            logger.debug("Conversation %s is already active; rejecting channel turn", conversation_id)
            return None
        activity_claimed = callable(begin_turn)
        try:
            if attachments:
                prepared = self._content_service.prepare_inputs(conversation, attachments, message_id=message.id)
                if prepared.failures:
                    self._content_service.cleanup_unreferenced(conversation, prepared.created_refs)
                    message.content += '\n[附件导入失败：请检查文件类型或大小后重新发送。]'
                    conversation.add_message(message)
                    self._conv_service.save(conversation, activity_token=claim_token)
                    replies = ChannelReplyCoordinator(channel=channel, platform_label=platform_label,
                        reply_normalizer=reply_normalizer, reply_sender=reply_sender)
                    return conversation, replies.failed_reply('频道附件无法导入，请检查文件类型或大小后重新发送。')
                message.content_refs = prepared.refs
            return self._process_resolved_message(
                channel,
                message,
                claim_token=claim_token,
                conversation=conversation,
                focus_requested=focus_requested,
                resolved_key=resolved_key,
                user_id=user_id,
                thread_id=thread_id,
                reply_user=reply_user,
                context_token=context_token,
                platform_label=platform_label,
                reply_normalizer=reply_normalizer,
                binding_updates=binding_updates,
                reply_sender=reply_sender,
            )
        finally:
            if activity_claimed:
                end_turn = getattr(self._conv_service, "end_turn", None)
                if callable(end_turn):
                    end_turn(conversation_id, claim_token)

    def _process_resolved_message(
        self,
        channel: ChannelConfig,
        message: Message,
        *,
        conversation: Conversation,
        claim_token: str | None,
        focus_requested: bool,
        resolved_key: str,
        user_id: str,
        thread_id: str,
        reply_user: str,
        context_token: str,
        platform_label: str,
        reply_normalizer: Callable[[str], str] | None,
        binding_updates: dict[str, Any] | None,
        reply_sender: Callable[[str, Message | None], None] | None,
    ) -> tuple[Conversation, str] | None:
        self._session_resolver.remember_binding_context(
            conversation,
            channel,
            user_id=user_id,
            thread_id=thread_id,
            reply_user=reply_user,
            context_token=context_token,
            binding_key=resolved_key,
            updates=binding_updates,
        )
        conversation.add_message(message)
        channel_id = str(channel.id or "").strip()
        conversation_id = str(getattr(conversation, "id", "") or "").strip()
        self._emit_event(
            ChannelEvent(
                kind="conversation-updated",
                channel_id=channel_id,
                conversation_id=conversation_id,
                source="channel-inbound",
                focus_requested=bool(focus_requested),
            )
        )

        request_id = str(uuid.uuid4())
        self._emit_event(
            ChannelEvent(
                kind="turn-started",
                channel_id=channel_id,
                conversation_id=conversation_id,
                source="channel-turn",
                request_id=request_id,
                payload={"platform": platform_label, "binding_key": resolved_key},
            )
        )
        replies = ChannelReplyCoordinator(
            channel=channel,
            platform_label=platform_label,
            reply_normalizer=reply_normalizer,
            reply_sender=reply_sender,
        )
        reply_text = ""
        final_message: Message | None = None
        try:
            provider = self._turn_runner.resolve_provider(conversation)
            if provider is None:
                raise RuntimeError("当前没有可用服务商，请先配置 Provider 和默认模型。")
            result = self._turn_runner.run_turn(
                provider=provider,
                conversation=conversation,
                policy=self._turn_runner.build_run_policy(conversation, channel),
                channel=channel,
                request_id=request_id,
                claim_token=claim_token,
                assistant_step_callback=replies.assistant_step_callback,
            )
            if replies.is_completed(result.status):
                final_message = result.final_message
                reply_text = replies.completed_reply(final_message)
            elif replies.is_cancelled(result.status):
                reply_text = replies.cancelled_reply()
                self._emit_error(channel_id, conversation_id, request_id, reply_text, "channel-cancelled")
            elif replies.is_interrupted(result.status):
                final_message = result.final_message
                reply_text = replies.interrupted_reply(
                    final_message,
                    stop_reason=result.stop_reason,
                    error=result.error or "",
                )
                self._emit_error(channel_id, conversation_id, request_id, reply_text, "channel-interrupted")
            else:
                reply_text = replies.failed_reply(result.error or "消息已收到，但生成回复时失败。")
                self._emit_error(channel_id, conversation_id, request_id, reply_text, "channel-failed")
        except Exception as exc:
            logger.exception("%s channel execution failed: %s", platform_label, exc)
            reply_text = replies.failed_reply(f"消息已收到，但当前处理失败：{exc}")
            self._emit_error(channel_id, conversation_id, request_id, reply_text, "channel-exception")
        self._emit_event(
            ChannelEvent(
                kind="turn-complete",
                channel_id=channel_id,
                conversation_id=conversation_id,
                source="channel-response",
                request_id=request_id,
                payload={"reply_text": reply_text, "message": final_message},
            )
        )
        self._emit_event(
            ChannelEvent(
                kind="conversation-updated",
                channel_id=channel_id,
                conversation_id=conversation_id,
                source="channel-response",
                request_id=request_id,
            )
        )
        return conversation, reply_text

    def _emit_error(
        self,
        channel_id: str,
        conversation_id: str,
        request_id: str,
        error: str,
        source: str,
    ) -> None:
        self._emit_event(
            ChannelEvent(
                kind="turn-error",
                channel_id=channel_id,
                conversation_id=conversation_id,
                source=source,
                request_id=request_id,
                payload={"error": error},
            )
        )




__all__ = ["ChannelMessageProcessor"]
