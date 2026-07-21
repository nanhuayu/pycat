from __future__ import annotations

from dataclasses import dataclass

from core.channel.catalog import ChannelCatalog, build_channel_catalog
from core.channel.platforms import ChannelPlatformBackend
from core.channel.platforms.feishu import FeishuChannelPlatformBackend
from core.channel.platforms.feishu.definition import FeishuChannelDefinition
from core.channel.platforms.feishu.delivery import FeishuDelivery
from core.channel.platforms.qqbot import QQBotChannelPlatformBackend
from core.channel.platforms.qqbot.definition import QQBotChannelDefinition
from core.channel.platforms.qqbot.delivery import QQBotDelivery
from core.channel.platforms.telegram import TelegramChannelPlatformBackend
from core.channel.platforms.telegram.definition import TelegramChannelDefinition
from core.channel.platforms.telegram.delivery import TelegramDelivery
from core.channel.platforms.wechat import WeChatChannelPlatformBackend
from core.channel.platforms.wechat.client import WeChatChannelClient
from core.channel.platforms.wechat.definition import WeChatChannelDefinition
from core.channel.platforms.wechat.delivery import WeChatDelivery
from core.channel.platforms.wechat.login import WeChatLoginFlow


@dataclass(frozen=True)
class ChannelPlatforms:
    catalog: ChannelCatalog
    backends: tuple[ChannelPlatformBackend, ...]
    wechat: WeChatDelivery
    feishu: FeishuDelivery
    qqbot: QQBotDelivery
    telegram: TelegramDelivery
    wechat_login: WeChatLoginFlow


def build_channel_platforms() -> ChannelPlatforms:
    wechat_client = WeChatChannelClient()
    wechat = WeChatDelivery(wechat_client)
    feishu = FeishuDelivery()
    qqbot = QQBotDelivery()
    telegram = TelegramDelivery()
    catalog = build_channel_catalog(
        (
            WeChatChannelDefinition(),
            QQBotChannelDefinition(),
            FeishuChannelDefinition(),
            TelegramChannelDefinition(),
        )
    )
    return ChannelPlatforms(
        catalog=catalog,
        wechat=wechat,
        feishu=feishu,
        qqbot=qqbot,
        telegram=telegram,
        wechat_login=WeChatLoginFlow(wechat_client),
        backends=(
            FeishuChannelPlatformBackend(feishu),
            QQBotChannelPlatformBackend(qqbot),
            TelegramChannelPlatformBackend(telegram),
            WeChatChannelPlatformBackend(wechat),
        ),
    )


__all__ = ["ChannelPlatforms", "build_channel_platforms"]
