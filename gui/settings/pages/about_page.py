from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel, QFrame

from gui.about_content import FEATURES, INTRO, LICENSE_LABEL, PRODUCT_NAME, REPOSITORY_LABEL, REPOSITORY_URL, TAGLINE
from gui.settings.page_header import build_page_header

class AboutPage(QWidget):
    page_title = "关于"

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(build_page_header(f"关于 {PRODUCT_NAME}", TAGLINE))

        card = QFrame()
        card.setObjectName("stat_card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 10, 12, 10)
        card_layout.setSpacing(6)
        card_layout.addWidget(QLabel(PRODUCT_NAME))
        detail = QLabel(INTRO)
        detail.setWordWrap(True)
        card_layout.addWidget(detail)
        layout.addWidget(card)

        features_card = QFrame()
        features_card.setObjectName("stat_card")
        features_layout = QVBoxLayout(features_card)
        features_layout.setContentsMargins(12, 10, 12, 10)
        features_layout.setSpacing(6)
        features_layout.addWidget(QLabel("能力概览"))
        for feature in FEATURES:
            label = QLabel(f"{feature.title}：{feature.description}")
            label.setWordWrap(True)
            features_layout.addWidget(label)
        layout.addWidget(features_card)

        repo = QLabel(f'<a href="{REPOSITORY_URL}">GitHub · {REPOSITORY_LABEL}</a>')
        repo.setOpenExternalLinks(True)
        repo.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        repo.setToolTip("打开 PyCat 开源仓库")
        layout.addWidget(QLabel(f"开源地址 · {LICENSE_LABEL}"))
        layout.addWidget(repo)

        layout.addStretch()
