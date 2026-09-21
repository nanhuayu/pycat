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
    layout.addWidget(title_label)

    description_text = str(description or "").strip()
    if description_text:
        layout.addWidget(build_page_description(description_text))

    return container
