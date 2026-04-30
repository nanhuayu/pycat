from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel, QFrame

from ui.settings.page_header import build_page_header

class AboutPage(QWidget):
    page_title = "关于"

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(build_page_header("关于 PyCat", "围绕统一运行时、工具契约和清晰设置结构构建的桌面 Agent。"))

        card = QFrame()
        card.setObjectName("stat_card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 10, 12, 10)
        card_layout.setSpacing(6)
        card_layout.addWidget(QLabel("当前产品重点"))
        detail = QLabel(
            "围绕统一运行时、集中状态投影和清晰分层，把模型、Agent、终端、MCP、技能与模式整理为更易发现的产品结构。"
        )
        detail.setWordWrap(True)
        card_layout.addWidget(detail)
        layout.addWidget(card)

        links = QLabel("项目重点：统一运行时、统一工具契约、可观测 Agent 流、分层 memory / 频道状态。")
        links.setWordWrap(True)
        layout.addWidget(links)

        repo = QLabel('<a href="https://github.com/nanhuayu/pycat">GitHub · nanhuayu/pycat</a>')
        repo.setOpenExternalLinks(True)
        repo.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        repo.setToolTip("打开 PyCat 开源仓库")
        layout.addWidget(QLabel("开源地址"))
        layout.addWidget(repo)

        layout.addStretch()
