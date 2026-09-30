"""Derive the small terminal mark from the canonical SVG (Qt is build-time only)."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtSvg import QSvgRenderer

ASSETS = Path(__file__).resolve().parents[1] / 'pycat/assets'


def generate() -> str:
    svg = ASSETS / 'pycat.svg'
    image = QImage(24, 24, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    QSvgRenderer(str(svg)).render(painter)
    painter.end()
    pixels = [[image.pixelColor(x, y).name() if image.pixelColor(x, y).alpha() >= 100 else ''
               for x in range(image.width())] for y in range(image.height())]
    return json.dumps({'source_sha256': hashlib.sha256(svg.read_bytes()).hexdigest(), 'pixels': pixels},
                      separators=(',', ':')) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    path = ASSETS / 'terminal-logo.json'
    value = generate()
    if args.check:
        if not path.exists() or path.read_text(encoding='utf-8') != value:
            parser.exit(1, 'Terminal logo is stale; run scripts/generate_terminal_logo.py.\n')
    else:
        path.write_text(value, encoding='utf-8')


if __name__ == '__main__':
    main()
