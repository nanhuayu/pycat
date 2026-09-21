from pycat.core.channel.catalog import ChannelDefinition, ChannelFieldDefinition, DeclarativeChannelDefinition


class DingTalkChannelDefinition(DeclarativeChannelDefinition):
    def __init__(self):
        super().__init__(ChannelDefinition(
            type='dingtalk', name='钉钉', default_name='钉钉频道', icon_name='comments',
            description='企业内部机器人，通过 Stream 长连接接收文字、图片和文件，无需公网回调地址。',
            default_config={'connection_mode': 'stream', 'channel_reply_policy': 'assistant_messages',
                            'send_thinking_to_channel': False},
            fields=(
                ChannelFieldDefinition('client_id', 'Client ID / AppKey', '企业内部应用 Client ID', required=True),
                ChannelFieldDefinition('client_secret', 'Client Secret / AppSecret', '应用密钥', required=True, secret=True),
                ChannelFieldDefinition('robot_code', 'Robot Code', '留空时使用 Client ID',
                    help_text='在钉钉开发者后台启用机器人，消息接收模式选择 Stream，并发布应用版本。'),
            ),
            summary_keys=('client_id',), tags=('钉钉', '企业办公', 'Stream'),
        ))
