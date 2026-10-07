from __future__ import annotations

from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget


def build_page_description(description: str) -> QLabel:
    """Return the muted description used below settings navigation."""

    label = QLabel(str(description or "").strip())
    label.setObjectName("settings_page_description")
    label.setWordWrap(True)
    label.setProperty("muted", True)
    return label


def build_page_header(title: str, description: str = "") -> QWidget:
    """Return a consistent title/description block for settings pages."""

    container = QWidget()
    container.setObjectName("settings_page_header")

    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)

    title_label = QLabel(str(title or "设置"))
    title_label.setObjectName("settings_page_title")
    title_label.setWordWrap(True)
    layout.addWidget(title_label)

    description_text = str(description or "").strip()
    if description_text:
        layout.addWidget(build_page_description(description_text))

    return container


def prepare_settings_page(page: QWidget, title: str) -> None:
    """Give a hosted page one canonical heading and shared outer spacing.

    Existing descriptions and editors remain in place. Specialized pages that
    omit a header receive the same small title block, without a second content
    container or any additional configuration state.
    """
    layout = page.layout()
    layout.setContentsMargins(24, 24, 24, 24)
    layout.setSpacing(16)
    header = page.findChild(QWidget, "settings_page_header")
    if header is None:
        layout.insertWidget(0, build_page_header(title))
        return
    header.findChild(QLabel, "settings_page_title").setText(title)
    header.show()
