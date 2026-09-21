from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from pycat.core.channel.envelope import channel_value
from pycat.core.channel.platforms.wechat.client import WeChatChannelClient
from pycat.core.channel.platforms.wechat.protocol import normalize_wechat_reply_text
from pycat.core.channel.platforms.wechat.router import normalize_wechat_ilink_message
from pycat.core.channel.platforms.wechat.media import MAX_MEDIA_ITEMS, WeChatMediaClient
from pycat.core.channel.replies import channel_reply_policy
from pycat.core.content.references import delivery_refs_for_messages
from pycat.core.content.session_content import MAX_INPUT_BATCH_BYTES
from pycat.models.contracts.channel import ChannelConfig
from pycat.models.conversation import Message


class WeChatDelivery:
    """WeChat reply delivery and normalized inbound binding."""

    def __init__(self, client: WeChatChannelClient | None = None, *, media=None, content_resolver=None) -> None:
        self._client = client or WeChatChannelClient()
        self._media = media or WeChatMediaClient()
        self._content_resolver = content_resolver

    @property
    def client(self) -> WeChatChannelClient:
        return self._client

    def enqueue_ilink_message(self, context: Any, channel: ChannelConfig, raw_message: Any) -> None:
        envelope = normalize_wechat_ilink_message(
            raw_message,
            mark_recent=lambda message_id: context.mark_recent_message(channel.id, message_id),
        )
        if envelope is not None:
            media = [item for item in raw_message.get('item_list', []) if isinstance(item, dict)
                     and str(item.get('type')) in ('2', '4')]
            kwargs = {'media': media} if media else {}
            context.enqueue_channel_message(channel, envelope.content, meta=envelope.meta, **kwargs)

    def process_message(self, context: Any, channel: ChannelConfig, message: Message) -> None:
        media = message.metadata.pop('_channel_media', [])
        user_id = channel_value(message, "user") or channel_value(message, "thread_id")
        reply_user = (
            channel_value(message, "reply_user")
            or str((channel.config or {}).get("receiver_id", "") or "").strip()
            or user_id
        )
        context_token = channel_value(message, "context_token")
        thread_id = channel_value(message, "thread_id") or user_id

        def _send_reply(content: str, _source_message: Message | None = None) -> None:
            self.send_reply(
                channel,
                touser=reply_user,
                content=content,
                context_token=context_token,
            )

        mode = str((channel.config or {}).get('connection_mode', 'ilink')).strip().lower()
        replies_enabled = channel_reply_policy(channel) not in {'none', 'silent', 'off'}
        with TemporaryDirectory(prefix='pycat-wechat-') if media else nullcontext(None) as temporary:
            attachments = []
            try:
                if len(media) > MAX_MEDIA_ITEMS:
                    raise ValueError('一次最多接收 8 个附件。')
                total = 0
                for item in media:
                    attachment = self._media.download(item, Path(temporary))
                    total += Path(attachment['path']).stat().st_size
                    if total > MAX_INPUT_BATCH_BYTES:
                        raise ValueError('附件合计超过 64 MiB。')
                    attachments.append(attachment)
            except (ValueError, OSError, RuntimeError):
                if replies_enabled:
                    _send_reply('微信附件接收失败，请检查文件并重新发送。每个文件上限 25 MiB，每次最多 8 个，合计 64 MiB。')
                return
            processed = context.process_bound_channel_message(
                channel, message, binding_key=user_id or thread_id, user_id=user_id,
                thread_id=thread_id, reply_user=reply_user, context_token=context_token,
                platform_label="WeChat", reply_normalizer=normalize_wechat_reply_text, reply_sender=_send_reply,
                binding_updates={'file_delivery': mode == 'ilink' and replies_enabled},
                attachments=attachments,
            )
        if processed and mode == 'ilink' and replies_enabled:
            self._send_outputs(channel, processed[0], message, reply_user, context_token)

    def _send_outputs(self, channel, conversation, anchor, recipient, context_token):
        start = next((index for index, item in enumerate(conversation.messages) if item.id == anchor.id), None)
        if start is None:
            return
        refs = delivery_refs_for_messages(conversation.messages[start + 1:])
        total = 0
        for index, ref in enumerate(refs):
            try:
                if index >= MAX_MEDIA_ITEMS:
                    raise ValueError('一次最多发送 8 个文件。')
                if self._content_resolver is None or ref.workspace != conversation.work_dir:
                    raise ValueError('交付文件不属于当前工作区。')
                path = self._content_resolver.resolve(conversation, ref)
                total += path.stat().st_size
                if total > MAX_INPUT_BATCH_BYTES:
                    raise ValueError('本轮文件合计超过 64 MiB。')
                item = self._media.upload(channel, recipient, path, expected_digest=ref.digest, name=ref.name)
                self._client.send_ilink_items(channel, touser=recipient, items=[item], context_token=context_token)
            except (ValueError, OSError, RuntimeError):
                self.send_reply(channel, touser=recipient,
                                content='部分交付文件未能发送，请在 PyCat 的本次产出中查看；文件需仍存在且未更改，每个上限 25 MiB，每次最多 8 个。',
                                context_token=context_token)
                break

    def send_reply(self, channel: ChannelConfig, *, touser: str, content: str, context_token: str = "") -> None:
        mode = str((channel.config or {}).get("connection_mode", "ilink") or "ilink").strip().lower()
        if mode == "ilink":
            self._client.send_ilink_reply(
                channel,
                touser=touser,
                content=content,
                context_token=context_token,
            )
            return

        app_id = str((channel.config or {}).get("app_id", "") or "").strip()
        app_secret = str((channel.config or {}).get("app_secret", "") or "").strip()
        if not (app_id and app_secret and touser):
            raise RuntimeError("微信公众号缺少 app_id / app_secret / touser，无法回发消息。")
        access_token = self._client.get_access_token(channel, app_id=app_id, app_secret=app_secret)
        self._client.send_official_reply(access_token, touser=touser, content=content)


__all__ = ["WeChatDelivery"]
