"""Localize owned extension metadata without changing resource payloads."""
from PyQt6.QtCore import QT_TRANSLATE_NOOP, QCoreApplication

# Qt extraction markers, keyed by identity so external names and descriptions
# never become UI text just because they contain a familiar Chinese word.
_PRESETS = {
    ('mcp', 'agent-browser'): {
        'title': QT_TRANSLATE_NOOP("ExtensionLabels", '浏览器 · agent-browser'),
        'description': QT_TRANSLATE_NOOP("ExtensionLabels", '原生驱动，无需 Node.js。复用本机 Chrome / Edge / Chromium，按对话隔离浏览器。'),
    },
    ('mcp', 'playwright'): {
        'title': QT_TRANSLATE_NOOP("ExtensionLabels", '浏览器 · Playwright MCP'),
        'description': QT_TRANSLATE_NOOP("ExtensionLabels", '可选浏览器后端，需要 Node.js 18+。通过 MCP 页配置和探测，程序更新由 npm 管理。'),
    },
    ('mcp', 'cua-driver'): {
        'title': QT_TRANSLATE_NOOP("ExtensionLabels", '桌面 · Cua Driver'),
        'description': QT_TRANSLATE_NOOP("ExtensionLabels", '可选跨平台桌面后端。Windows / Linux 使用驱动，macOS 需官方签名应用和系统授权；本期不自动安装。'),
    },
    ('mcp', 'mcp-registry'): {
        'title': QT_TRANSLATE_NOOP("ExtensionLabels", 'MCP 官方目录'),
        'description': QT_TRANSLATE_NOOP("ExtensionLabels", '查找更多 MCP Server，再在 MCP 页导入配置。目录信息不代表 PyCat 已验证该程序。'),
    },
    ('skill', 'skills-directory'): {
        'title': QT_TRANSLATE_NOOP("ExtensionLabels", 'Skills 目录'),
        'description': QT_TRANSLATE_NOOP("ExtensionLabels", '查找技能的原始 GitHub 仓库和目录，通过本页固定提交安装并检查更新。'),
    },
}

_STATUS = frozenset((
    QT_TRANSLATE_NOOP("ExtensionLabels", '未安装'),
    QT_TRANSLATE_NOOP("ExtensionLabels", '已配置'),
    QT_TRANSLATE_NOOP("ExtensionLabels", '自定义配置'),
    QT_TRANSLATE_NOOP("ExtensionLabels", '外部管理'),
    QT_TRANSLATE_NOOP("ExtensionLabels", '在线目录'),
    QT_TRANSLATE_NOOP("ExtensionLabels", '已启用'),
    QT_TRANSLATE_NOOP("ExtensionLabels", '已停用'),
    QT_TRANSLATE_NOOP("ExtensionLabels", '市场条目'),
    QT_TRANSLATE_NOOP("ExtensionLabels", '待安装'),
    QT_TRANSLATE_NOOP("ExtensionLabels", '已安装'),
))
_SCOPES = frozenset((
    QT_TRANSLATE_NOOP("ExtensionLabels", 'PyCat 内置'),
    QT_TRANSLATE_NOOP("ExtensionLabels", '用户目录'),
    QT_TRANSLATE_NOOP("ExtensionLabels", '项目目录'),
    QT_TRANSLATE_NOOP("ExtensionLabels", '外部目录'),
))
_MARKET_SKILL_DESCRIPTION = QT_TRANSLATE_NOOP("ExtensionLabels", "来自 skills.sh 的公开目录。准备安装时核对 GitHub 目录并固定提交。")
_CONFIGURED_MCP_DESCRIPTION = QT_TRANSLATE_NOOP("ExtensionLabels", "已配置的 MCP。请通过原安装方式更新程序；远程服务由服务端更新。工具列表可在 MCP 页重新探测。")


def extension_text(row: dict, field: str) -> str:
    source = str(row.get(field) or "")
    owned = False
    if field == "status":
        owned = source in _STATUS
    elif field == "source":
        owned = row.get("kind") == "skill" and row.get("installed") and source in _SCOPES
    elif field in {"title", "description"}:
        if row.get("kind") == "skill" and row.get("installed"):
            return source
        preset = _PRESETS.get((row.get("kind"), row.get("id")), {})
        owned = bool(source) and source == preset.get(field)
        if field == "description":
            owned |= row.get("kind") == "skill" and row.get("management") == "market" and source == _MARKET_SKILL_DESCRIPTION
            owned |= row.get("kind") == "mcp" and str(row.get("id", "")).startswith("configured:") and source == _CONFIGURED_MCP_DESCRIPTION
    return QCoreApplication.translate("ExtensionLabels", source) if owned else source
