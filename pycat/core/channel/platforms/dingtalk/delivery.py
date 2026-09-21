from pycat.core.channel.envelope import channel_value
from pycat.core.channel.media import ChannelMediaTransfer
from pycat.core.channel.platforms.dingtalk.client import DingTalkChannelClient


class DingTalkDelivery:
    def __init__(self, client=None, *, content_resolver=None):
        self._client = client or DingTalkChannelClient()
        self._media = ChannelMediaTransfer(content_resolver)

    def process_message(self, context, channel, message):
        recipient = channel_value(message, 'reply_user')
        user = channel_value(message, 'user')
        thread = channel_value(message, 'thread_id')
        kind = channel_value(message, 'conversation_type')
        def reply(text, _source=None):
            self.send_reply(channel, receive_id=recipient, content=text, conversation_type=kind)
        return self._media.process(context, channel, message,
            binding_key=thread, user_id=user, thread_id=thread, reply_user=recipient,
            context_token='', platform_label='钉钉',
            binding_updates={'conversation_type': kind},
            reply_normalizer=self._client.normalize_reply_text, reply_sender=reply,
            download=lambda item, directory: self._client.download_media(channel, item, directory),
            send_file=lambda file: self._client.send_file(channel, receive_id=recipient, conversation_type=kind, file=file))

    def send_reply(self, channel, *, receive_id, content, conversation_type='1'):
        self._client.send_text_message(channel, receive_id=receive_id, text=content, conversation_type=conversation_type)
