"""Localized tool labels; policy and schema identities stay in the model layer."""
from PyQt6.QtCore import QT_TRANSLATE_NOOP, QCoreApplication

_CATEGORY_LABELS = {
    "read": QT_TRANSLATE_NOOP("ToolingLabels", "读取"),
    "web": QT_TRANSLATE_NOOP("ToolingLabels", "联网"),
    "edit": QT_TRANSLATE_NOOP("ToolingLabels", "编辑"),
    "execute": QT_TRANSLATE_NOOP("ToolingLabels", "执行"),
    "state": QT_TRANSLATE_NOOP("ToolingLabels", "状态与交互"),
    "delegate": QT_TRANSLATE_NOOP("ToolingLabels", "委托"),
    "capability": QT_TRANSLATE_NOOP("ToolingLabels", "能力"),
    "mcp": "MCP",
}


def tool_category_label(category: str) -> str:
    source = _CATEGORY_LABELS.get(category)
    return QCoreApplication.translate("ToolingLabels", source) if source is not None else category


def risk_level_label(level: str) -> str:
    return {
        "low": QCoreApplication.translate("ToolingLabels", "低风险"),
        "medium": QCoreApplication.translate("ToolingLabels", "中风险"),
        "high": QCoreApplication.translate("ToolingLabels", "高风险"),
    }.get(level, level)


# Extraction markers for owned metadata; IDs and custom titles are never translated.
_BUILTIN_NAMES = {
    'agent__complete': 'Complete task',
    'agent__run': 'Run sub-agent',
    'agent__task': QT_TRANSLATE_NOOP("ToolingLabels", '独立任务'),
    'archive__list': QT_TRANSLATE_NOOP("ToolingLabels", '列出会话归档'),
    'archive__read': QT_TRANSLATE_NOOP("ToolingLabels", '读取会话归档'),
    'file__delete': QT_TRANSLATE_NOOP("ToolingLabels", '删除文件'),
    'file__deliver': QT_TRANSLATE_NOOP("ToolingLabels", '交付文件'),
    'file__edit': QT_TRANSLATE_NOOP("ToolingLabels", '精确编辑文件'),
    'file__list': QT_TRANSLATE_NOOP("ToolingLabels", '列出文件'),
    'file__ocr': QT_TRANSLATE_NOOP("ToolingLabels", '提取图片文字'),
    'file__patch': QT_TRANSLATE_NOOP("ToolingLabels", '应用补丁'),
    'file__read': QT_TRANSLATE_NOOP("ToolingLabels", '读取文件'),
    'file__search': QT_TRANSLATE_NOOP("ToolingLabels", '搜索文件'),
    'file__write': QT_TRANSLATE_NOOP("ToolingLabels", '写入文件'),
    'python__exec': QT_TRANSLATE_NOOP("ToolingLabels", '运行 Python'),
    'shell__kill': QT_TRANSLATE_NOOP("ToolingLabels", '终止后台进程'),
    'shell__list': QT_TRANSLATE_NOOP("ToolingLabels", '列出后台进程'),
    'shell__read': QT_TRANSLATE_NOOP("ToolingLabels", '读取后台进程'),
    'shell__run': QT_TRANSLATE_NOOP("ToolingLabels", '运行命令'),
    'shell__write': QT_TRANSLATE_NOOP("ToolingLabels", '输入终端'),
    'skill__manage': QT_TRANSLATE_NOOP("ToolingLabels", '提出技能候选'),
    'state__artifact': QT_TRANSLATE_NOOP("ToolingLabels", '管理会话产物'),
    'state__memory': QT_TRANSLATE_NOOP("ToolingLabels", '管理记忆'),
    'state__todo': QT_TRANSLATE_NOOP("ToolingLabels", '更新任务进度'),
    'state__wiki': QT_TRANSLATE_NOOP("ToolingLabels", '项目知识'),
    'user__ask': QT_TRANSLATE_NOOP("ToolingLabels", '询问用户'),
    'web__fetch': QT_TRANSLATE_NOOP("ToolingLabels", '读取网页'),
    'web__search': QT_TRANSLATE_NOOP("ToolingLabels", '搜索网页'),
}

_CAPABILITY_NAMES = {
    'image': QT_TRANSLATE_NOOP("ToolingLabels", '图像生成与编辑'),
    'ocr': QT_TRANSLATE_NOOP("ToolingLabels", '视觉 OCR'),
    'prompt_optimize': QT_TRANSLATE_NOOP("ToolingLabels", '提示词优化'),
    'title': QT_TRANSLATE_NOOP("ToolingLabels", '标题提取'),
    'compress': QT_TRANSLATE_NOOP("ToolingLabels", '上下文压缩'),
    'memory_review': QT_TRANSLATE_NOOP("ToolingLabels", '记忆与知识整理'),
    'summarize': QT_TRANSLATE_NOOP("ToolingLabels", '文本总结'),
    'wiki_synthesize': QT_TRANSLATE_NOOP("ToolingLabels", '整理项目知识'),
}


def tool_name_label(name: str) -> str:
    """Label a canonical invocation ID without guessing names of external tools."""
    source = _BUILTIN_NAMES.get(name)
    if name.startswith('capability__'):
        source = _CAPABILITY_NAMES.get(name.removeprefix('capability__'))
    return QCoreApplication.translate('ToolingLabels', source) if source else name


def tool_display_name(descriptor) -> str:
    source = _BUILTIN_NAMES.get(descriptor.name)
    if descriptor.source == "capability":
        source = _CAPABILITY_NAMES.get(descriptor.name.removeprefix("capability__"))
    if descriptor.source in {"builtin", "search", "capability"} and source and descriptor.display_name == source:
        return QCoreApplication.translate("ToolingLabels", source)
    return descriptor.display_name or descriptor.name


def capability_display_name(capability) -> str:
    source = _CAPABILITY_NAMES.get(capability.id)
    if source and capability.name == source:
        return QCoreApplication.translate("ToolingLabels", source)
    return capability.name
