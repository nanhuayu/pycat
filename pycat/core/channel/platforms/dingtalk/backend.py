from pycat.core.channel.connection import ChannelConnectionSnapshot, ChannelConnectionState
from pycat.core.channel.platforms.base import ChannelPlatformBackend
from pycat.core.channel.platforms.dingtalk.delivery import DingTalkDelivery
from pycat.core.channel.platforms.dingtalk.stream import start_dingtalk_stream


class DingTalkChannelPlatformBackend(ChannelPlatformBackend):
    channel_type = 'dingtalk'

    def __init__(self, delivery=None):
        self._delivery = delivery or DingTalkDelivery()

    def connection_snapshot(self, context, channel):
        if not channel.enabled:
            state, detail = ChannelConnectionState.DISABLED, '频道已停用。'
        elif not all(str(channel.config.get(key) or '').strip() for key in ('client_id', 'client_secret')):
            state, detail = ChannelConnectionState.INCOMPLETE, '缺少 Client ID 或 Client Secret。'
        else:
            state, detail = ChannelConnectionState.CONNECTING, '应用设置后启动钉钉 Stream。'
        return ChannelConnectionSnapshot(channel.id, 'dingtalk', mode='stream', state=state, detail=detail)

    def start(self, context, channel):
        snapshot = self.connection_snapshot(context, channel)
        context.report_connection(snapshot)
        if snapshot.state == ChannelConnectionState.CONNECTING:
            context.remember_connection_handle(start_dingtalk_stream(context, channel))

    def process_message(self, context, channel, message):
        self._delivery.process_message(context, channel, message)

    def send_bound_message(self, context, channel, conversation, *, text, reply_user, context_token):
        binding = conversation.settings.get('channel_binding') or {}
        self._delivery.send_reply(channel, receive_id=reply_user, content=text,
            conversation_type=str(binding.get('conversation_type') or '1'))
        return True
