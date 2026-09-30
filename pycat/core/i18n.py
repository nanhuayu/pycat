"""Headless UI translations from the same source catalog used by Qt.

Translate owned presentation strings at their call sites, never arbitrary
payloads. This module has no GUI dependency and owns no language preference.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

CATALOG = Path(__file__).resolve().parents[1] / 'assets' / 'translations' / 'pycat_en.ts'
CONTEXT = 'Workbench'


def normalize_language(language: str) -> str:
    return 'en' if language == 'en' else 'zh_CN'


@lru_cache(maxsize=1)
def _messages() -> dict[str, str]:
    try:
        return {message.findtext('source', ''): message.findtext('translation', '')
                for context in ET.parse(CATALOG).findall('context') if context.findtext('name') == CONTEXT
                for message in context.findall('message')
                if message.find('translation') is not None and not message.find('translation').get('type')}
    except (OSError, ET.ParseError):
        logging.getLogger(__name__).warning('UI translation catalog is unavailable; using source text.')
        return {}


@dataclass(frozen=True)
class Translator:
    language: str = 'zh_CN'

    def __call__(self, source: str, **values) -> str:
        text = (_messages().get(source) or source) if self.language == 'en' else source
        return text.format(**values) if values else text


def ui_catalog(language: str) -> dict:
    selected = normalize_language(language)
    return {'language': selected, 'messages': dict(_messages()) if selected == 'en' else {}}
