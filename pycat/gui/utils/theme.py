"""Small theme helpers and UI tokens for the PyQt GUI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from string import Template

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QColor, QIcon, QPalette
from PyQt6.QtWidgets import QApplication, QMenu, QToolButton, QWidget

from pycat.models.contracts.config import DEFAULT_ACCENT


@dataclass(frozen=True)
class ThemeTokens:
    """Compact token set shared by QSS, custom painting, and icon calls."""

    colors: dict[str, str]
    spacing: dict[str, int]
    radius: dict[str, int]
    font_size: dict[str, int]
    icon_size: dict[str, int]

    def color(self, name: str, default: str = "") -> str:
        return self.colors.get(name, default)

    def space(self, name: str, default: int = 0) -> int:
        return int(self.spacing.get(name, default))

    def corner(self, name: str, default: int = 0) -> int:
        return int(self.radius.get(name, default))

    def font(self, name: str, default: int = 13) -> int:
        return int(self.font_size.get(name, default))

    def icon(self, name: str, default: int = 18) -> int:
        return int(self.icon_size.get(name, default))


THEME_COLORS: dict[str, dict[str, str]] = {
    "light": {
        # Keep the light workbench on one white canvas.  Subtle hierarchy is
        # expressed by ``surface_alt`` and borders instead of gray page bands.
        "window": "#ffffff",
        "surface": "#ffffff",
        "surface_alt": "#f7f8fa",
        "border": "#e5e7eb",
        "text": "#202428",
        "muted": "#68717a",
        "primary": "#7567c7",
        "error": "#c73e4d",
        "success": "#2f7d4a",
    },
    "dark": {
        "window": "#17191c",
        "surface": "#202327",
        "surface_alt": "#282c31",
        "border": "#3a4047",
        "text": "#edf0f2",
        "muted": "#a3abb3",
        "primary": "#a99bff",
        "error": "#ef7d8a",
        "success": "#76c58d",
    },
}

ACCENT_COLORS: dict[str, dict[str, str]] = {
    "blue": {
        "light": "#287baf",
        "dark": "#74b9e6",
    },
    "lavender": {
        "light": "#7567c7",
        "dark": "#a99bff",
    },
    "forest": {
        "light": "#34805b",
        "dark": "#81c6a0",
    },
}

UI_SPACING: dict[str, int] = {
    "xs": 4,
    "sm": 8,
    "md": 8,
    "lg": 12,
    "xl": 16,
    "panel": 16,
    "card_x": 12,
    "card_y": 8,
}

# Shared compact row height for settings controls and expandable chat headers.
# Keeping this in the existing theme token module avoids per-widget geometry
# constants drifting apart as the UI is tightened.
COMPACT_CONTROL_HEIGHT = 30
COMPACT_ICON_BUTTON_SIZE = 28
INSPECTOR_MARGIN = 6


def configure_icon_button(button: QToolButton, icon: QIcon, label: str) -> None:
    """Keep compact commands readable by keyboard and easy to click."""
    button.setIcon(icon)
    button.setProperty("compactCommand", True)
    button.setText(label)
    button.setToolTip(label)
    button.setAccessibleName(label)
    button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
    button.setAutoRaise(True)
    button.setIconSize(QSize(16, 16))
    button.setFixedSize(COMPACT_ICON_BUTTON_SIZE, COMPACT_ICON_BUTTON_SIZE)
    button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

UI_RADIUS: dict[str, int] = {
    "xs": 3,
    "sm": 4,
    "md": 6,
    "lg": 8,
    "button": 6,
    "card": 8,
}

UI_FONT_SIZE: dict[str, int] = {
    "caption": 12,
    "body": 13,
    "heading": 18,
    "mono": 12,
}

UI_ICON_SIZE: dict[str, int] = {
    "sm": 16,
    "md": 18,
    "lg": 20,
    "nav": 18,
    "hero": 32,
}

# Terminal ANSI colors have stable semantic meanings independent of app accent.
TERMINAL_COLORS = {
    "background": "#171b23", "foreground": "#d7dae0", "selection": "#354b6d",
    "cursor": "#89b4fa", "inactive_cursor": "#626c7c",
    "black": "#20232b", "red": "#e06c75", "green": "#98c379", "brown": "#e5c07b",
    "blue": "#61afef", "magenta": "#c678dd", "cyan": "#56b6c2", "white": "#d7dae0",
    "brightblack": "#727987", "brightred": "#ff8790", "brightgreen": "#b1e593",
    "brightbrown": "#ffe09a", "brightblue": "#82cfff", "brightmagenta": "#e5a6ff",
    "brightcyan": "#7de5e9", "brightwhite": "#ffffff",
}


def normalize_theme(theme: object) -> str:
    return "dark" if str(theme or "").strip().lower() == "dark" else "light"


def normalize_accent(accent: object) -> str:
    value = str(accent or "").strip().lower()
    return value if value in ACCENT_COLORS else DEFAULT_ACCENT


def _blend(background: str, foreground: str, amount: float) -> str:
    bg = QColor(background)
    fg = QColor(foreground)
    alpha = max(0.0, min(1.0, float(amount)))
    color = QColor(
        round(bg.red() * (1.0 - alpha) + fg.red() * alpha),
        round(bg.green() * (1.0 - alpha) + fg.green() * alpha),
        round(bg.blue() * (1.0 - alpha) + fg.blue() * alpha),
    )
    return color.name(QColor.NameFormat.HexRgb)


def theme_colors(theme: object, accent: object = DEFAULT_ACCENT) -> dict[str, str]:
    normalized = normalize_theme(theme)
    colors = dict(THEME_COLORS[normalized])
    colors["primary"] = ACCENT_COLORS[normalize_accent(accent)][normalized]
    dark = normalized == "dark"
    surface = colors["surface"]
    border = colors["border"]
    primary = colors["primary"]
    error = colors["error"]
    colors.update(
        {
            "surface_hover": _blend(surface, primary, 0.11 if dark else 0.055),
            "control_border": border,
            "primary_strong": _blend(primary, colors["text"], 0.14 if dark else 0.18),
            "primary_hover": _blend(surface, primary, 0.18 if dark else 0.09),
            "selected": _blend(surface, primary, 0.22 if dark else 0.13),
            "user_message_bg": _blend(surface, primary, 0.16 if dark else 0.11),
            "selected_text": colors["text"] if dark else _blend(primary, colors["text"], 0.24),
            "selected_meta": primary,
            "selected_border": _blend(border, primary, 0.32),
            "text_selection": primary,
            "text_selection_text": "#ffffff",
            "disabled": _blend(surface, colors["muted"], 0.72),
            "danger_bg": _blend(surface, error, 0.17 if dark else 0.07),
            "danger_border": _blend(border, error, 0.34),
            "danger_hover": _blend(surface, error, 0.25 if dark else 0.13),
            "warning": "#d6a94a" if dark else "#b7791f",
            "on_primary": "#ffffff",
            "markdown_code_bg": colors["surface_alt"],
            "markdown_code_text": colors["text"],
            "markdown_quote": primary,
            "markdown_link": primary,
            # Reading surface for document/image previews: stays light in both
            # themes so file previews never render on a near-black backdrop.
            "preview_surface": "#f6f7f9" if dark else "#ffffff",
            "preview_text": "#1f2328",
        }
    )
    return colors


def theme_tokens(theme: object, accent: object = DEFAULT_ACCENT) -> ThemeTokens:
    return ThemeTokens(
        colors=theme_colors(theme, accent),
        spacing=UI_SPACING,
        radius=UI_RADIUS,
        font_size=UI_FONT_SIZE,
        icon_size=UI_ICON_SIZE,
    )


def render_theme_stylesheet(
    theme: object,
    template_path: str | Path,
    accent: object = DEFAULT_ACCENT,
) -> str:
    """Render the single theme QSS template from the shared color tokens."""

    normalized = normalize_theme(theme)
    template = Path(template_path).resolve()
    icon_dir = template.parent.parent / "icons"
    values = dict(theme_colors(normalized, accent))
    values.update(
        {
            "transparent": "transparent",
            "chevron_icon": str(
                icon_dir / ("chevron-down-light.svg" if normalized == "dark" else "chevron-down-dark.svg")
            ).replace("\\", "/"),
            "checkbox_check_icon": str(icon_dir / "checkbox-check.svg").replace("\\", "/"),
        }
    )
    content = template.read_text(encoding="utf-8")
    return Template(content).safe_substitute(values)


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


def resolve_accent(widget: QWidget | None = None) -> str:
    current = widget
    while current is not None:
        value = str(current.property("accent") or "").strip().lower()
        if value in ACCENT_COLORS:
            return value
        current = current.parentWidget()

    app = QApplication.instance()
    value = str(app.property("accent") or "").strip().lower() if app is not None else ""
    return normalize_accent(value)


def palette_for_theme(theme: object, accent: object = DEFAULT_ACCENT) -> QPalette:
    colors = theme_colors(theme, accent)
    palette = QPalette()
    role_map = {
        QPalette.ColorRole.Window: colors["window"],
        QPalette.ColorRole.WindowText: colors["text"],
        QPalette.ColorRole.Base: colors["surface"],
        QPalette.ColorRole.AlternateBase: colors["surface_alt"],
        QPalette.ColorRole.Text: colors["text"],
        QPalette.ColorRole.Button: colors["surface"],
        QPalette.ColorRole.ButtonText: colors["text"],
        QPalette.ColorRole.Highlight: colors["text_selection"],
        QPalette.ColorRole.HighlightedText: colors["text_selection_text"],
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


def context_menu_stylesheet(theme: object, accent: object = DEFAULT_ACCENT) -> str:
    colors = theme_colors(theme, accent)
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
    accent = resolve_accent(owner)
    menu.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    menu.setPalette(palette_for_theme(theme, accent))
    menu.setStyleSheet(context_menu_stylesheet(theme, accent))
    return menu
