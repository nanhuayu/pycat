"""Channel names and connection states projected for the desktop UI."""
from PyQt6.QtCore import QT_TRANSLATE_NOOP, QCoreApplication

from pycat.core.channel.catalog import ChannelDefinition
from pycat.core.channel.connection import ChannelConnectionSnapshot, ChannelConnectionState

# These are extraction markers for Qt-free catalog metadata, not a second
# translation dictionary. Never pass channel names, config values or diagnostics here.
_METADATA_SOURCES = frozenset((
    QT_TRANSLATE_NOOP("ChannelStatus", '个人微信 iLink 支持文字、图片接收和文件收发，单文件 25 MiB；公众号 Webhook 仅文字。'),
    QT_TRANSLATE_NOOP("ChannelStatus", '二维码'),
    QT_TRANSLATE_NOOP("ChannelStatus", '代理地址'),
    QT_TRANSLATE_NOOP("ChannelStatus", '企业内部应用 Client ID'),
    QT_TRANSLATE_NOOP("ChannelStatus", '企业内部机器人，通过 Stream 长连接接收文字、图片和文件，无需公网回调地址。'),
    QT_TRANSLATE_NOOP("ChannelStatus", '企业办公'),
    QT_TRANSLATE_NOOP("ChannelStatus", '可选：socks5:// 或 https://'),
    QT_TRANSLATE_NOOP("ChannelStatus", '可选：固定回发目标或测试会话，例如 -100xxxxxxxxxx'),
    QT_TRANSLATE_NOOP("ChannelStatus", '可选：当前仅预留，暂不启用 AES 解密'),
    QT_TRANSLATE_NOOP("ChannelStatus", '可选：暂未实现加密回调解密'),
    QT_TRANSLATE_NOOP("ChannelStatus", '可选：覆盖默认回发用户（一般留空）'),
    QT_TRANSLATE_NOOP("ChannelStatus", '回调 Token'),
    QT_TRANSLATE_NOOP("ChannelStatus", '回调校验 Token'),
    QT_TRANSLATE_NOOP("ChannelStatus", '回调路径'),
    QT_TRANSLATE_NOOP("ChannelStatus", '国内场景'),
    QT_TRANSLATE_NOOP("ChannelStatus", '在钉钉开发者后台启用机器人，消息接收模式选择 Stream，并发布应用版本。'),
    QT_TRANSLATE_NOOP("ChannelStatus", '如需固定回发目标，可填写'),
    QT_TRANSLATE_NOOP("ChannelStatus", '应用密钥'),
    QT_TRANSLATE_NOOP("ChannelStatus", '开发者'),
    QT_TRANSLATE_NOOP("ChannelStatus", '开放平台地址'),
    QT_TRANSLATE_NOOP("ChannelStatus", '微信'),
    QT_TRANSLATE_NOOP("ChannelStatus", '机器人'),
    QT_TRANSLATE_NOOP("ChannelStatus", '沙箱环境'),
    QT_TRANSLATE_NOOP("ChannelStatus", '用于验证回调'),
    QT_TRANSLATE_NOOP("ChannelStatus", '留空时使用 Client ID'),
    QT_TRANSLATE_NOOP("ChannelStatus", '监听地址'),
    QT_TRANSLATE_NOOP("ChannelStatus", '监听端口'),
    QT_TRANSLATE_NOOP("ChannelStatus", '目标用户 ID'),
    QT_TRANSLATE_NOOP("ChannelStatus", '目标群组 / 会话 ID'),
    QT_TRANSLATE_NOOP("ChannelStatus", '社区'),
    QT_TRANSLATE_NOOP("ChannelStatus", '请输入 App Secret'),
    QT_TRANSLATE_NOOP("ChannelStatus", '跨平台'),
    QT_TRANSLATE_NOOP("ChannelStatus", '通过 QQ 官方 Gateway 长连接接入机器人，适合 QQ 群聊、单聊和频道问答。'),
    QT_TRANSLATE_NOOP("ChannelStatus", '通过 Telegram Bot API 长轮询接入机器人，只需 Bot Token；Chat ID 可用于手动测试或固定回发目标。'),
    QT_TRANSLATE_NOOP("ChannelStatus", '钉钉'),
    QT_TRANSLATE_NOOP("ChannelStatus", '飞书'),
    QT_TRANSLATE_NOOP("ChannelStatus", '默认使用飞书开放平台中国站地址；国际版可改为 Lark 域名。'),
    QT_TRANSLATE_NOOP("ChannelStatus", '默认使用飞书长连接模式直连开放平台，也兼容高级场景下的 webhook 回调模式。'),
))


def channel_metadata_text(source: str) -> str:
    if source not in _METADATA_SOURCES:
        return source
    return QCoreApplication.translate("ChannelStatus", source)


def channel_type_name(definition: ChannelDefinition) -> str:
    return channel_metadata_text(definition.name)


# Only connector-owned, fixed notices are translated. Diagnostics and account
# data remain exact evidence; these markers never change the service snapshot.
_WECHAT_NOTICES = frozenset((
    QT_TRANSLATE_NOOP("ChannelStatus", "个人微信消息连接已启动。"),
    QT_TRANSLATE_NOOP("ChannelStatus", "请先完成个人微信扫码连接。"),
    QT_TRANSLATE_NOOP("ChannelStatus", "个人微信登录已失效，请重新扫码。"),
    QT_TRANSLATE_NOOP("ChannelStatus", "二维码已生成，请使用手机微信扫码。"),
    QT_TRANSLATE_NOOP("ChannelStatus", "二维码已过期，请重新生成。"),
    QT_TRANSLATE_NOOP("ChannelStatus", "等待手机微信扫码。"),
    QT_TRANSLATE_NOOP("ChannelStatus", "二维码已扫描，请在手机上确认连接。"),
    QT_TRANSLATE_NOOP("ChannelStatus", "验证码多次错误，本次二维码已被阻止，请重新生成。"),
    QT_TRANSLATE_NOOP("ChannelStatus", "扫码已确认，正在切换微信服务节点。"),
    QT_TRANSLATE_NOOP("ChannelStatus", "该微信账号已绑定，但当前连接没有可复用凭据，请删除后重新添加。"),
    QT_TRANSLATE_NOOP("ChannelStatus", "该微信账号已经连接。"),
    QT_TRANSLATE_NOOP("ChannelStatus", "微信确认成功，但服务端返回的连接凭据不完整。"),
    QT_TRANSLATE_NOOP("ChannelStatus", "个人微信连接成功。"),
    QT_TRANSLATE_NOOP("ChannelStatus", "微信公众号 Webhook 已启动。"),
    QT_TRANSLATE_NOOP("ChannelStatus", "微信公众号 Webhook 启动失败，请检查监听地址和端口。"),
))


def channel_detail_label(snapshot: ChannelConnectionSnapshot) -> str:
    if snapshot.channel_type == "wechat" and snapshot.detail in _WECHAT_NOTICES:
        return QCoreApplication.translate("ChannelStatus", snapshot.detail)
    return snapshot.detail


def channel_state_label(state: ChannelConnectionState) -> str:
    return {
        ChannelConnectionState.DISABLED: QCoreApplication.translate("ChannelStatus", "已停用"),
        ChannelConnectionState.INCOMPLETE: QCoreApplication.translate("ChannelStatus", "配置不完整"),
        ChannelConnectionState.CONNECTING: QCoreApplication.translate("ChannelStatus", "连接中"),
        ChannelConnectionState.WAITING_USER: QCoreApplication.translate("ChannelStatus", "等待操作"),
        ChannelConnectionState.READY: QCoreApplication.translate("ChannelStatus", "已连接"),
        ChannelConnectionState.RECONNECTING: QCoreApplication.translate("ChannelStatus", "正在重连"),
        ChannelConnectionState.ERROR: QCoreApplication.translate("ChannelStatus", "异常"),
    }.get(state, state.value)
