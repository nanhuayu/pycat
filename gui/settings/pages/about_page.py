from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from core.app.services.release import ReleaseCheckResult, STABLE_RELEASE_PAGE
from core.version import __version__
from gui.about_content import (
    FEATURES,
    INTRO,
    LICENSE_LABEL,
    PRODUCT_NAME,
    RELEASES_URL,
    REPOSITORY_LABEL,
    REPOSITORY_URL,
    TAGLINE,
)
from gui.settings.page_header import build_page_header
from gui.utils.icon_manager import Icons

class AboutPage(QWidget):
    page_title = "关于"
    check_requested = pyqtSignal()
    release_open_requested = pyqtSignal(str)
    release_ignore_requested = pyqtSignal(str)

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
        repo.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        repo.setToolTip("打开 PyCat 开源仓库")
        layout.addWidget(QLabel(f"开源地址 · {LICENSE_LABEL}"))
        layout.addWidget(repo)

        release_card = QFrame()
        release_card.setObjectName("stat_card")
        release_layout = QVBoxLayout(release_card)
        release_layout.setContentsMargins(12, 10, 12, 10)
        release_layout.setSpacing(6)

        release_layout.addWidget(QLabel("版本与更新"))
        self.version_label = QLabel(f"当前版本 v{__version__}")
        self.version_label.setObjectName("about_version_label")
        release_layout.addWidget(self.version_label)

        self.release_status_label = QLabel("尚未检查更新")
        self.release_status_label.setObjectName("about_release_status")
        self.release_status_label.setProperty("muted", True)
        self.release_status_label.setWordWrap(True)
        release_layout.addWidget(self.release_status_label)

        release_actions = QHBoxLayout()
        release_actions.setContentsMargins(0, 0, 0, 0)
        release_actions.setSpacing(6)
        self.check_update_btn = QPushButton("检查更新")
        self.check_update_btn.setObjectName("about_check_update_btn")
        self.check_update_btn.setIcon(Icons.get(Icons.REFRESH))
        self.check_update_btn.clicked.connect(self.check_requested.emit)
        release_actions.addWidget(self.check_update_btn)

        self.open_release_btn = QPushButton("打开 Release")
        self.open_release_btn.setObjectName("about_open_release_btn")
        self.open_release_btn.setIcon(Icons.get(Icons.EXTERNAL_OPEN))
        self.open_release_btn.setVisible(False)
        self.open_release_btn.clicked.connect(self._open_current_release)
        release_actions.addWidget(self.open_release_btn)

        self.ignore_release_btn = QPushButton("忽略此版本")
        self.ignore_release_btn.setObjectName("about_ignore_release_btn")
        self.ignore_release_btn.setVisible(False)
        self.ignore_release_btn.clicked.connect(self._ignore_current_release)
        release_actions.addWidget(self.ignore_release_btn)
        release_actions.addStretch(1)
        release_layout.addLayout(release_actions)
        layout.addWidget(release_card)

        layout.addStretch()

        self._release = None

    def set_release_checking(self, checking: bool) -> None:
        self.check_update_btn.setEnabled(not checking)
        self.check_update_btn.setText("检查中..." if checking else "检查更新")

    def set_release_result(self, result: ReleaseCheckResult | None, *, ignored_tag: str = "") -> None:
        self._release = getattr(result, "release", None) if result is not None else None
        self.open_release_btn.setVisible(False)
        self.ignore_release_btn.setVisible(False)
        if result is None:
            self.release_status_label.setText("尚未检查更新")
            return
        status = str(getattr(result, "status", "error") or "error")
        if status == "available" and self._release is not None:
            version = str(getattr(self._release, "tag_name", "") or "").strip()
            title = str(getattr(self._release, "name", "") or "").strip()
            detail = f"发现新版本 {version}"
            if title and title != version:
                detail += f"：{title}"
            published_at = str(getattr(self._release, "published_at", "") or "").strip()
            if published_at:
                detail += f"（{published_at[:10]}）"
            if str(ignored_tag or "").strip() == version:
                detail += "，已忽略"
            else:
                self.open_release_btn.setVisible(True)
                self.ignore_release_btn.setVisible(True)
            self.release_status_label.setText(detail)
            self.open_release_btn.setEnabled(bool(getattr(self._release, "html_url", "")))
            return
        if status == "up_to_date":
            current = str(getattr(result, "current_version", "") or __version__).strip()
            self.release_status_label.setText(f"已是最新稳定版本 v{current}")
            return
        self.release_status_label.setText(str(getattr(result, "error", "") or "暂时无法检查更新"))

    def _open_current_release(self) -> None:
        release = self._release
        url = str(getattr(release, "html_url", "") or RELEASES_URL).strip()
        self.release_open_requested.emit(url if url.startswith("https://") else STABLE_RELEASE_PAGE)

    def _ignore_current_release(self) -> None:
        release = self._release
        tag = str(getattr(release, "tag_name", "") or "").strip()
        if tag:
            self.release_ignore_requested.emit(tag)
