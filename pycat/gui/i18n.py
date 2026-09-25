"""Install native Qt catalogs once, before creating the desktop widgets.

The saved language takes effect on the next launch. No widget traversal,
runtime text replacement, extra settings reader or live-window rebuild.
"""
from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import QLibraryInfo, QTranslator

from pycat.models.contracts.config import AppConfig

logger = logging.getLogger(__name__)
TRANSLATIONS = Path(__file__).resolve().parents[1] / "assets" / "translations"


def install_language(app, language: str) -> str:
    """Return the actual UI language; retain translators for the app lifetime."""
    selected = AppConfig.from_dict({"language": language}).language
    translators = []
    if selected == "en":
        translator = QTranslator(app)
        if translator.load(str(TRANSLATIONS / "pycat_en.qm")):
            translators.append(translator)
        else:
            translator.deleteLater()
            logger.warning("English catalog could not be loaded; using Chinese UI.")
            selected = "zh_CN"
    if selected == "zh_CN":
        translator = QTranslator(app)
        path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        if translator.load("qtbase_zh_CN", path):
            translators.append(translator)
        else:
            translator.deleteLater()
            logger.warning("Qt's Chinese catalog is unavailable; standard Qt controls use English.")

    for previous in getattr(app, "_pycat_translators", ()):
        app.removeTranslator(previous)
        previous.deleteLater()
    app._pycat_translators = tuple(translators)
    for translator in translators:
        app.installTranslator(translator)
    app.setProperty("ui_language", selected)
    return selected
