from __future__ import annotations

from dataclasses import dataclass
from html import escape


PRODUCT_NAME = "PyCat"
TAGLINE = "LLM 对话、Agent 和工具协作的桌面工作台"
INTRO = "PyCat 把对话、工作区、模型、工具调用、记忆和产物放在同一条工作流里。选好工作区和模型，直接写下目标。"
CHAT_EMPTY_TITLE = "PyCat 工作台"
CHAT_EMPTY_DESCRIPTION = "选好工作区和模型，直接写下目标。PyCat 会把对话、工具、文件和记忆串到同一条上下文里。"
REPOSITORY_URL = "https://github.com/nanhuayu/pycat"
REPOSITORY_LABEL = "nanhuayu/pycat"
LICENSE_LABEL = "AGPL-3.0"


@dataclass(frozen=True)
class AboutFeature:
    title: str
    description: str
    icon_name: str


FEATURES: tuple[AboutFeature, ...] = (
    AboutFeature("工作区与文件", "让对话、文件引用和命令执行落在同一处。", "folder"),
    AboutFeature("工具与产物", "把工具调用、修改记录和生成产物留在清晰的上下文里。", "wrench"),
    AboutFeature("记忆与频道", "保留有用线索，并用频道承接更长的工作流。", "brain"),
)


def about_dialog_html() -> str:
    items = "".join(
        f"<li><b>{escape(feature.title)}</b>：{escape(feature.description)}</li>"
        for feature in FEATURES
    )
    return (
        f"<h2>{escape(PRODUCT_NAME)}</h2>"
        f"<p>{escape(TAGLINE)}</p>"
        f"<p>{escape(INTRO)}</p>"
        f"<ul>{items}</ul>"
        f"<p><a href=\"{escape(REPOSITORY_URL)}\">GitHub · {escape(REPOSITORY_LABEL)}</a></p>"
        f"<p>许可证：{escape(LICENSE_LABEL)}</p>"
    )
