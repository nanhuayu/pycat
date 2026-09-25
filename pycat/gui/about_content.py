from __future__ import annotations

from dataclasses import dataclass
from html import escape

from PyQt6.QtCore import QT_TRANSLATE_NOOP, QCoreApplication

from pycat.core.app.services.release import STABLE_RELEASE_PAGE
from pycat.core.version import __version__

PRODUCT_NAME = "PyCat"
TAGLINE = QT_TRANSLATE_NOOP("AboutContent", "LLM 对话、Agent 和工具协作的桌面工作台")
INTRO = QT_TRANSLATE_NOOP("AboutContent", "PyCat 把对话、工作区、模型、工具调用、记忆和产物放在同一条工作流里。选好工作区和模型，直接写下目标。")
CHAT_EMPTY_TITLE = "PyCat 工作台"
CHAT_EMPTY_DESCRIPTION = "选好工作区和模型，直接写下目标。PyCat 会把对话、工具、文件和记忆串到同一条上下文里。"
REPOSITORY_URL = "https://github.com/nanhuayu/pycat"
REPOSITORY_LABEL = "nanhuayu/pycat"
RELEASES_URL = STABLE_RELEASE_PAGE
LICENSE_LABEL = "AGPL-3.0"


@dataclass(frozen=True)
class AboutFeature:
    title: str
    description: str
    icon_name: str


FEATURES: tuple[AboutFeature, ...] = (
    AboutFeature(QT_TRANSLATE_NOOP("AboutContent", "工作区与文件"), QT_TRANSLATE_NOOP("AboutContent", "让对话、文件引用和命令执行落在同一处。"), "folder"),
    AboutFeature(QT_TRANSLATE_NOOP("AboutContent", "工具与产物"), QT_TRANSLATE_NOOP("AboutContent", "把工具调用、修改记录和生成产物留在清晰的上下文里。"), "wrench"),
    AboutFeature(QT_TRANSLATE_NOOP("AboutContent", "记忆与频道"), QT_TRANSLATE_NOOP("AboutContent", "保留有用线索，并用频道承接更长的工作流。"), "brain"),
)


def about_dialog_html() -> str:
    def text(source: str) -> str:
        return escape(QCoreApplication.translate("AboutContent", source))

    items = "".join(
        f"<li><b>{text(feature.title)}</b>: {text(feature.description)}</li>"
        for feature in FEATURES
    )
    version = QCoreApplication.translate("AboutContent", "版本：v{version}").format(version=__version__)
    license_text = QCoreApplication.translate("AboutContent", "许可证：{license}").format(license=LICENSE_LABEL)
    release_text = QCoreApplication.translate("AboutContent", "查看 Release")
    return (
        f"<h2>{escape(PRODUCT_NAME)}</h2>"
        f"<p>{text(TAGLINE)}</p>"
        f"<p>{escape(version)}</p>"
        f"<p>{text(INTRO)}</p>"
        f"<ul>{items}</ul>"
        f"<p><a href=\"{escape(REPOSITORY_URL)}\">GitHub · {escape(REPOSITORY_LABEL)}</a></p>"
        f"<p><a href=\"{escape(RELEASES_URL)}\">{escape(release_text)}</a></p>"
        f"<p>{escape(license_text)}</p>"
    )
