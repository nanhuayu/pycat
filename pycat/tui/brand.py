"""Rich character projection of the bundled PyCat mark; no image/GUI runtime."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from rich.style import Style
from rich.text import Text


@lru_cache(maxsize=1)
def _pixels():
    return json.loads((Path(__file__).resolve().parents[1] / 'assets/terminal-logo.json').read_text(encoding='utf-8'))['pixels']


def terminal_logo(*, encoding='utf-8') -> Text:
    try:
        '▀▄'.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return Text(' /\\_/\\\n( >.< )\n \\___/\\', style='bold blue')
    pixels = _pixels()
    result = Text()
    for row in range(0, len(pixels), 2):
        if row:
            result.append('\n')
        for top, bottom in zip(pixels[row], pixels[row + 1]):
            if top:
                result.append('▀', Style(color=top, bgcolor=bottom or None))
            elif bottom:
                result.append('▄', Style(color=bottom))
            else:
                result.append(' ')
    return result
