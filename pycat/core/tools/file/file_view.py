"""Explicit visual input for the current model, separate from text extraction and OCR."""
import asyncio
import hashlib
import json
from typing import Any

from pycat.core.content.images import MAX_IMAGE_BYTES, prepare_view_image
from pycat.core.content.pdf import MAX_PDF_BYTES, render_pdf_pages_isolated
from pycat.core.tools.base import BaseTool, ToolContext, ToolResult
from pycat.core.tools.file.file_source import resolve_file_source


class ViewFileTool(BaseTool):
    @property
    def name(self) -> str:
        return 'file__view'

    @property
    def display_name(self) -> str:
        return '查看图像'

    @property
    def category(self) -> str:
        return 'read'

    @property
    def description(self) -> str:
        return ('Show a static image or selected PDF pages to the current image-capable model. '
                'No OCR or auxiliary model call. Images default to a 2048-pixel longest side; '
                'use detail=original for raster pixels within size limits. '
                'PDFs render at up to 144 DPI / 2048 pixels, default one page, maximum four. '
                'Use file__read for native text and file__ocr for transcription.')

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            'type': 'object', 'properties': {
                'path': {'type': 'string', 'description': 'Authorized file, input:<id>, or archive:<id>/images/<number>.'},
                'start_page': {'type': 'integer', 'minimum': 1, 'description': 'PDF only: 1-based start page; default 1.'},
                'page_count': {'type': 'integer', 'minimum': 1, 'maximum': 4, 'description': 'PDF only: default 1, maximum 4.'},
                'detail': {'type': 'string', 'enum': ['auto', 'original'], 'description': 'Raster images only: auto resizes; original preserves pixel dimensions. Default auto.'},
            }, 'required': ['path'], 'additionalProperties': False,
        }

    def requested_read_path(self, arguments: dict[str, Any]) -> str:
        path = str(arguments.get('path') or '').strip()
        return '' if path.startswith(('input:', 'archive:')) else path.removeprefix('workspace:')

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.provider is not None:
            model = str(getattr(context.conversation, 'model', '') or '')
            if not context.provider.effective_model_profile(model).supports_input('image'):
                return ToolResult('The current model does not support image input. Select a vision model or use file__ocr for text.', is_error=True)
        path = str(arguments.get('path') or '').strip()
        if not path:
            return ToolResult('path is required.', is_error=True)
        try:
            source = await asyncio.to_thread(resolve_file_source, path, context, archive_images=True, max_bytes=MAX_IMAGE_BYTES)
            detail = str(arguments.get('detail') or 'auto')
            if detail not in {'auto', 'original'}:
                raise ValueError('detail must be auto or original.')
            is_pdf = source.mime == 'application/pdf' or source.name.lower().endswith('.pdf')
            limit = MAX_PDF_BYTES if is_pdf else MAX_IMAGE_BYTES
            if source.path.stat().st_size > limit:
                raise ValueError(f'Source exceeds {limit // (1024 * 1024)} MiB; use a smaller source.')
            if is_pdf:
                if detail != 'auto':
                    raise ValueError('detail=original applies to raster images; PDF pages use bounded rendering.')
                payload = await asyncio.to_thread(render_pdf_pages_isolated, source.path,
                    start_page=int(arguments.get('start_page', 1)), page_count=int(arguments.get('page_count', 1)))
                urls = [page.pop('data_url') for page in payload['images']]
            else:
                if arguments.get('start_page', 1) != 1 or arguments.get('page_count', 1) != 1:
                    raise ValueError('Page ranges apply only to PDF files.')
                payload, urls = await asyncio.to_thread(self._image_view, source.path, detail)
            payload['source_ref'] = path
            blocks = [{'type': 'text', 'text': json.dumps(payload, ensure_ascii=False)}]
            blocks.extend({'type': 'image', 'mimeType': url[5:].split(';', 1)[0], 'data': url} for url in urls)
            return ToolResult(blocks, metadata={
                'auto_summary': False, 'source_ref': path,
                'source_digest': payload.get('source_digest') or source.digest,
            })
        except (OSError, ValueError, TypeError) as exc:
            return ToolResult(f'View error: {exc}', is_error=True)

    @staticmethod
    def _image_view(path, detail: str) -> tuple[dict, list[str]]:
        with path.open('rb') as stream:
            data = stream.read(MAX_IMAGE_BYTES + 1)
        raster, info = prepare_view_image(data, detail=detail)
        return {'images': [info], 'source_digest': hashlib.sha256(data).hexdigest()}, [raster.data_url]
