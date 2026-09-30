"""Bounded text pages, including lossless continuation within a long line."""
from __future__ import annotations

import time

MAX_TEXT_CHARS = 64000
MAX_TEXT_LINES = 2000
READ_TIMEOUT_SECONDS = 10


def read_text_page(path, *, start_line=1, end_line=None, start_column=1):
    with path.open('r', encoding='utf-8', errors='replace') as stream:
        return read_text_stream(stream, start_line=start_line, end_line=end_line, start_column=start_column)


def read_text_stream(stream, *, start_line=1, end_line=None, start_column=1):
    start = int(start_line)
    column = int(start_column)
    end = int(end_line) if end_line is not None else start + MAX_TEXT_LINES - 1
    if start < 1 or end < start or column < 1:
        raise ValueError('Invalid line range or start_column; use 1-based positions and end_line >= start_line.')
    end = min(end, start + MAX_TEXT_LINES - 1)
    deadline = time.monotonic() + READ_TIMEOUT_SECONDS
    line, position, total = 1, 1, 0
    parts, count = [], 0
    eof = False
    last_line = None
    while line <= end:
        if time.monotonic() >= deadline:
            raise TimeoutError('Text read timed out while locating the range; use a smaller source or a targeted search.')
        # readline(size) also bounds memory for minified JSON and very long PGN lines.
        skipping = line < start or (line == start and position < column)
        size = MAX_TEXT_CHARS if skipping else MAX_TEXT_CHARS - count
        if line == start and position < column:
            size = min(size, column - position)
        chunk = stream.readline(size)
        if not chunk:
            eof = True
            break
        total = line
        if not skipping:
            parts.append(chunk)
            count += len(chunk)
            last_line = line
        if chunk.endswith('\n'):
            if line == start and position + len(chunk) < column:
                raise ValueError('start_column exceeds the requested line length.')
            line += 1
            position = 1
        else:
            position += len(chunk)
        if count >= MAX_TEXT_CHARS or line > end:
            eof = not stream.read(1)
            break
    text = ''.join(parts)
    metadata = {'start_line': start, 'start_column': column, 'end_line': last_line,
                'eof': eof, 'next_line': None if eof else line,
                'next_column': None if eof else position}
    if eof:
        metadata['total_lines'] = total
    if not text:
        note = 'File is empty.' if total == 0 else f'End of file: {total} lines; no text at the requested position.'
        return note, metadata
    ranged = start != 1 or column != 1 or end_line is not None
    if not ranged and eof:
        return text, metadata
    label = f'Lines {start}-{last_line}' + (f' of {total}' if eof else '')
    if column != 1:
        label += f' (starting at column {column})'
    rendered = f'{label}:\n{text}'
    if not eof:
        rendered += f'\n\nContinue with start_line={line}, start_column={position}.'
    return rendered, metadata
