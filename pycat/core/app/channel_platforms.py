from __future__ import annotations

from dataclasses import dataclass

from pycat.core.channel.catalog import ChannelCatalog, build_channel_catalog
from pycat.core.channel.platforms import ChannelPlatformBackend
from pycat.core.channel.platforms.dingtalk.backend import DingTalkChannelPlatformBackend
from pycat.core.channel.platforms.dingtalk.definition import DingTalkChannelDefinition
from pycat.core.channel.platforms.dingtalk.delivery import DingTalkDelivery
from pycat.core.channel.platforms.feishu import FeishuChannelPlatformBackend
from pycat.core.channel.platforms.feishu.definition import FeishuChannelDefinition
from pycat.core.channel.platforms.feishu.delivery import FeishuDelivery
from pycat.core.channel.platforms.qqbot import QQBotChannelPlatformBackend
from pycat.core.channel.platforms.qqbot.definition import QQBotChannelDefinition
from pycat.core.channel.platforms.qqbot.delivery import QQBotDelivery
from pycat.core.channel.platforms.telegram import TelegramChannelPlatformBackend
from pycat.core.channel.platforms.telegram.definition import TelegramChannelDefinition
from pycat.core.channel.platforms.telegram.delivery import TelegramDelivery
from pycat.core.channel.platforms.wechat import WeChatChannelPlatformBackend
from pycat.core.channel.platforms.wechat.client import WeChatChannelClient
from pycat.core.channel.platforms.wechat.definition import WeChatChannelDefinition
from pycat.core.channel.platforms.wechat.delivery import WeChatDelivery
from pycat.core.channel.platforms.wechat.login import WeChatLoginFlow


@dataclass(frozen=True)
class ChannelPlatforms:
    catalog: ChannelCatalog
    backends: tuple[ChannelPlatformBackend, ...]
    wechat: WeChatDelivery
    feishu: FeishuDelivery
    qqbot: QQBotDelivery
    telegram: TelegramDelivery
    dingtalk: DingTalkDelivery
    wechat_login: WeChatLoginFlow


def build_channel_platforms(*, content_resolver=None) -> ChannelPlatforms:
    wechat_client = WeChatChannelClient()
    wechat = WeChatDelivery(wechat_client, content_resolver=content_resolver)
    feishu = FeishuDelivery(content_resolver=content_resolver)
    qqbot = QQBotDelivery(content_resolver=content_resolver)
    telegram = TelegramDelivery(content_resolver=content_resolver)
    dingtalk = DingTalkDelivery(content_resolver=content_resolver)
    catalog = build_channel_catalog(
        (
            WeChatChannelDefinition(),
            QQBotChannelDefinition(),
            FeishuChannelDefinition(),
            TelegramChannelDefinition(),
            DingTalkChannelDefinition(),
        )
    )
    return ChannelPlatforms(
        catalog=catalog,
        wechat=wechat,
        feishu=feishu,
        qqbot=qqbot,
        telegram=telegram,
        dingtalk=dingtalk,
        wechat_login=WeChatLoginFlow(wechat_client),
        backends=(
            FeishuChannelPlatformBackend(feishu),
            QQBotChannelPlatformBackend(qqbot),
            TelegramChannelPlatformBackend(telegram),
            WeChatChannelPlatformBackend(wechat),
            DingTalkChannelPlatformBackend(dingtalk),
        ),
    )


__all__ = ["ChannelPlatforms", "build_channel_platforms"]
