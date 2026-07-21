from __future__ import annotations

from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget


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
        description_label = QLabel(description_text)
        description_label.setObjectName("settings_page_description")
        description_label.setWordWrap(True)
        description_label.setProperty("muted", True)
        layout.addWidget(description_label)

    return container