"""Bounded native PDF text; rendering and OCR remain explicit separate operations."""
import json
import os
import subprocess
from pathlib import Path

from pycat.core.hosts.python import resolve_python_runner, run_python_code

MAX_TEXT_CHARS = 64_000
MAX_PDF_BYTES = 20 * 1024 * 1024


def extract_pdf_text_isolated(path: Path, *, start_page: int = 1, page_count: int = 5) -> dict:
    """Keep MuPDF out of the application's concurrent preview/OCR threads."""
    package_root = Path(__file__).resolve().parents[3]
    code = (
        f'import sys\nsys.path.insert(0, {str(package_root)!r})\n'
        'import json\nfrom pathlib import Path\n'
        'from pycat.core.content.pdf import extract_pdf_text\n'
        f'print(json.dumps(extract_pdf_text(Path({str(path.resolve())!r}), '
        f'start_page={start_page!r}, page_count={page_count!r}), ensure_ascii=False))\n'
    )
    try:
        result = run_python_code(
            code=code,
            # Built-in parsing must use PyCat's dependencies, not a user override.
            python_runner=resolve_python_runner(use_configured=False),
            cwd=package_root,
            timeout_sec=30,
            env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'},
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError('PDF extraction timed out after 30s; use a smaller page batch.') from exc
    if result.returncode != 0:
        detail = result.stderr.decode('utf-8', errors='replace').strip().splitlines()
        raise ValueError('PDF extraction failed: ' + (detail[-1][:1000] if detail else f'exit {result.returncode}'))
    return json.loads(result.stdout.decode('utf-8'))


def extract_pdf_text(path: Path, *, start_page: int = 1, page_count: int = 5) -> dict:
    # Load the native backend only at the explicit worker operation. Importing
    # the transport above for the tool catalog must not initialize MuPDF.
    import pymupdf

    if start_page < 1 or not 1 <= page_count <= 32:
        raise ValueError('PDF start_page must be >= 1 and page_count must be between 1 and 32.')
    if path.stat().st_size > MAX_PDF_BYTES:
        raise ValueError('PDF file is larger than 20 MB; use a smaller source.')
    with pymupdf.open(path) as document:
        if not document.is_pdf:
            raise ValueError('Source is not a PDF document.')
        if document.needs_pass:
            raise ValueError('PDF is encrypted; provide an unlocked copy to read it.')
        total = len(document)
        if start_page > total:
            raise ValueError(f'PDF start_page {start_page} exceeds page count {total}.')
        pages = []
        remaining = MAX_TEXT_CHARS
        for index in range(start_page - 1, min(total, start_page - 1 + page_count)):
            page = document[index]
            text = page.get_text('text', sort=True)
            # Do not partially consume another page merely because the batch is full.
            if pages and len(text) > remaining:
                break
            pages.append({
                'page': index + 1,
                'text': text[:remaining],
                'images': len(page.get_images()),
                'text_truncated': len(text) > remaining,
            })
            remaining -= min(len(text), remaining)
            if remaining == 0:
                break
        last = pages[-1]['page']
        return {
            'page_count': total,
            'pages': pages,
            'next_page': last + 1 if last < total else None,
            'notice': (
                'Native text only; images and vector outlines have not been OCRed. '
                'Use file__ocr on relevant pages/images for missing text, and inspect table reading order. '
                'Follow next_page for remaining pages. If text_truncated is true, that page is incomplete; '
                'use Python with pymupdf for a narrower extraction before claiming completeness.'
            ),
        }
