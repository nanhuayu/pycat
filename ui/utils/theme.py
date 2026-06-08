"""Small theme helpers for custom-painted widgets and native-ish popups."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication, QMenu, QWidget


THEME_COLORS: dict[str, dict[str, str]] = {
    "light": {
        "window": "#ffffff",
        "app_bg": "#f7f6fb",
        "surface": "#ffffff",
        "surface_alt": "#fbf9ff",
        "text": "#1f1f29",
        "muted": "#6f6a7c",
        "border": "#e7e2f2",
        "control_border": "#ddd8eb",
        "primary": "#7c3aed",
        "primary_hover": "#f3edff",
        "selected": "#efe7ff",
        "selected_text": "#4c1d95",
        "selected_meta": "#6d28d9",
        "selected_border": "#d8cef1",
        "disabled": "#a09aaa",
    },
    "dark": {
        "window": "#1c1c22",
        "app_bg": "#16161a",
        "surface": "#202028",
        "surface_alt": "#24242e",
        "text": "#ececf1",
        "muted": "#a1a1ad",
        "border": "#30303a",
        "control_border": "#343440",
        "primary": "#8b5cf6",
        "primary_hover": "#332b47",
        "selected": "#332b47",
        "selected_text": "#ffffff",
        "selected_meta": "#ddd6fe",
        "selected_border": "#514a70",
        "disabled": "#6f6f7a",
    },
}


def normalize_theme(theme: object) -> str:
    return "dark" if str(theme or "").strip().lower() == "dark" else "light"


def theme_colors(theme: object) -> dict[str, str]:
    return THEME_COLORS[normalize_theme(theme)]


def resolve_theme(widget: QWidget | None = None) -> str:
    current = widget
    while current is not None:
        value = str(current.property("theme") or "").strip().lower()
        if value in {"light", "dark"}:
            return value
        current = current.parentWidget()

    app = QApplication.instance()
    value = str(app.property("theme") or "").strip().lower() if app is not None else ""
    if value in {"light", "dark"}:
        return value

    palette = widget.palette() if widget is not None else (app.palette() if app is not None else QPalette())
    return "dark" if palette.color(QPalette.ColorRole.Window).lightness() < 128 else "light"


def palette_for_theme(theme: object) -> QPalette:
    colors = theme_colors(theme)
    palette = QPalette()
    role_map = {
        QPalette.ColorRole.Window: colors["window"],
        QPalette.ColorRole.WindowText: colors["text"],
        QPalette.ColorRole.Base: colors["surface"],
        QPalette.ColorRole.AlternateBase: colors["surface_alt"],
        QPalette.ColorRole.Text: colors["text"],
        QPalette.ColorRole.Button: colors["surface"],
        QPalette.ColorRole.ButtonText: colors["text"],
        QPalette.ColorRole.Highlight: colors["selected"],
        QPalette.ColorRole.HighlightedText: colors["selected_text"],
        QPalette.ColorRole.ToolTipBase: colors["surface"],
        QPalette.ColorRole.ToolTipText: colors["text"],
        QPalette.ColorRole.PlaceholderText: colors["muted"],
        QPalette.ColorRole.Mid: colors["control_border"],
    }
    for role, value in role_map.items():
        palette.setColor(role, QColor(value))
    disabled_text = QColor(colors["disabled"])
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.PlaceholderText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, disabled_text)
    return palette


def context_menu_stylesheet(theme: object) -> str:
    colors = theme_colors(theme)
    return f"""
        QMenu {{
            background-color: {colors["window"]};
            color: {colors["text"]};
            border: 1px solid {colors["border"]};
            padding: 0px;
            border-radius: 0px;
        }}
        QMenu::item {{
            background-color: transparent;
            color: {colors["text"]};
            padding: 5px 18px;
            margin: 0px;
            border-radius: 0px;
        }}
        QMenu::item:selected {{
            background-color: {colors["primary_hover"]};
            color: {colors["selected_text"]};
        }}
        QMenu::item:disabled {{
            color: {colors["disabled"]};
        }}
        QMenu::separator {{
            height: 10px;
            margin: 0px;
            padding: 0px;
            border: none;
            background-color: {colors["window"]};
            border-top: 1px solid {colors["border"]};
        }}
    """


def prepare_context_menu(menu: QMenu, owner: QWidget | None = None) -> QMenu:
    theme = resolve_theme(owner)
    menu.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    menu.setPalette(palette_for_theme(theme))
    menu.setStyleSheet(context_menu_stylesheet(theme))
    return menu
