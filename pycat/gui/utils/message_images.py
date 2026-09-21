"""Project registered image deliveries into ordinary Markdown, without new UI."""
from collections.abc import Iterable
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote

import markdown
from PyQt6.QtCore import QUrl

from pycat.models.contracts.content import ContentRef


def image_key(ref: ContentRef) -> tuple[str, str]:
    return (ref.mime, ref.digest) if len(ref.digest) == 64 else (ref.kind, ref.ref)


def image_source(value: object) -> str:
    value = unquote(str(value or ''))
    if value.startswith('file:'):
        value = QUrl(value).toLocalFile()
    value = value.replace('\\', '/')
    # QUrl normalizes a Windows drive letter as a URL scheme.
    if len(value) > 2 and value[1:3] == ':/':
        value = value[0].lower() + value[1:]
    return value[2:] if value.startswith('./') else value


class _ImageSources(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.sources = []
        self.feed(markdown.markdown(text, extensions=['fenced_code', 'tables']))

    def handle_starttag(self, tag, attrs):
        if tag == 'img':
            self.sources.append(image_source(dict(attrs).get('src', '')))


def message_image_markdown(
    text: str, refs: Iterable[ContentRef], deliveries: Iterable[ContentRef] = (),
) -> tuple[str, dict[str, ContentRef], set[tuple[str, str]]]:
    """Return Markdown, its allowed image aliases, and images shown in the body."""
    images = [ref for ref in refs if ref.mime.startswith('image/')]
    chosen = {}
    for ref in images:
        key = image_key(ref)
        if key not in chosen or ref.kind == 'workspace':
            chosen[key] = ref
    aliases = {}
    for ref in images:
        selected = chosen[image_key(ref)]
        names = [ref.ref, ref.name]
        if ref.kind == 'workspace':
            relative = ref.ref.removeprefix('workspace:')
            names.append(relative)
            if ref.workspace and Path(ref.workspace).is_absolute():
                names.append(str(Path(ref.workspace) / relative))
        for name in names:
            key = image_source(name)
            aliases[key] = selected if key not in aliases or aliases[key] == selected else None
    aliases = {key: ref for key, ref in aliases.items() if ref is not None}
    shown = {
        image_key(aliases[source]) for source in _ImageSources(text).sources if source in aliases
    } if images else set()
    for ref in deliveries:
        key = image_key(ref)
        if key in chosen and key not in shown and len(shown) < 4:
            selected = chosen[key]
            label = selected.name.replace('\\', '\\\\').replace('[', '\\[').replace(']', '\\]')
            text += f'\n\n![{label}](<{quote(selected.ref, safe=":/")}>)'
            shown.add(key)
    return text, aliases, shown
