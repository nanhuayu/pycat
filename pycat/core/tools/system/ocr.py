"""Permission-aware file adapter for the shared OCR workflow."""
from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from pycat.core.content.ocr import OcrService, OcrError
from pycat.core.content.resolver import ResolvedContent, SessionContentResolver
from pycat.core.tools.base import BaseTool, ToolContext, ToolResult
from pycat.models.contracts.content import ContentRef

_RETRYABLE_ERRORS = {"source_changed", "inference_failed"}


class FileOcrTool(BaseTool):
    def __init__(self, service: OcrService) -> None:
        self.service = service

    @property
    def name(self) -> str:
        return "file__ocr"


    @property
    def display_name(self) -> str:
        return "提取图片文字"


    @property
    def description(self) -> str:
        return (
            f"Extract text using the configured {'vision model service' if self.service.config.backend == 'vision' else 'local PP-OCR'} "
            "from an authorized image/PDF or current-session input/archive image reference. "
            "PDFs are processed in page batches and return next_page when more pages remain."
        )


    @property
    def category(self) -> str:
        return "read"


    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Authorized local/workspace path, input:<id>, or archive:<id>/images/<number> in this session.",
                },
                "start_page": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "1-based PDF start page; default 1.",
                },
                "page_count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 32,
                    "description": "PDF pages in this batch; defaults to the saved OCR setting.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        }


    def requested_read_path(self, arguments: dict[str, Any]) -> str:
        path = str(arguments.get("path") or "").strip()
        return "" if path.startswith(("input:", "archive:")) else path.removeprefix("workspace:")


    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path_text = str(arguments.get("path") or "").strip()
        if not path_text:
            return self._error("invalid_argument", "path is required.")
        try:
            start_page = int(arguments.get("start_page", 1))
            page_count_value = arguments.get("page_count")
            page_count = None if page_count_value is None else int(page_count_value)
        except (TypeError, ValueError):
            return self._error("invalid_argument", "start_page 和 page_count 必须是整数。")
        if start_page < 1 or (page_count is not None and not 1 <= page_count <= 32):
            return self._error("invalid_argument", "页码从 1 开始，page_count 必须在 1 到 32 之间。")

        try:
            source = self._resolve_source(path_text, context)
        except PermissionError as exc:
            return self._error("source_denied", str(exc))
        except FileNotFoundError as exc:
            return self._error("source_not_found", str(exc))
        except (OSError, ValueError) as exc:
            message = str(exc)
            code = "source_denied" if "outside workspace" in message or "Access denied" in message else "source_not_found"
            return self._error(code, message)

        cancel_event = getattr(getattr(context, "runtime", None), "cancel_event", None)
        try:
            result = await self.service.recognize(
                source.path,
                source_ref=source.ref.ref,
                source_digest=source.digest,
                source_name=source.name,
                source_mime=source.mime,
                start_page=start_page,
                page_count=page_count,
                cancel_event=cancel_event,
            )
        except OcrError as exc:
            return self._error(exc.code, str(exc), source_ref=source.ref.ref)
        except Exception:
            return self._error("inference_failed", "OCR 执行失败。", source_ref=source.ref.ref)
        return ToolResult(result.to_text(), metadata=result.metadata())


    @staticmethod
    def _resolve_source(path_text: str, context: ToolContext) -> ResolvedContent:
        if path_text.startswith(("input:", "archive:")):
            if context.conversation is None:
                raise ValueError("input references require an active conversation.")
            content_service = getattr(context, "content_service", None)
            if content_service is None:
                raise ValueError("input references require the session content service.")
            return SessionContentResolver(content_service).resolve_content(
                context.conversation,
                path_text,
            )

        path = context.resolve_read_path(path_text.removeprefix("workspace:"))
        if context.files:
            ref = ContentRef(id=str(path), name=path.name, mime=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                size=0, digest="", ref=f"workspace:{path}", kind="workspace", workspace=context.work_dir)
            return SessionContentResolver(context.content_service).resolve_content(context.conversation, ref)
        if not path.is_file():
            raise FileNotFoundError(path_text)
        raw_work_dir = str(context.work_dir or "").strip()
        name = path.name
        relative = ""
        if raw_work_dir:
            try:
                relative = path.relative_to(
                    Path(raw_work_dir).expanduser().resolve()
                ).as_posix()
            except ValueError:
                relative = ""
        if relative:
            ref = ContentRef(
                id=relative,
                name=name,
                mime=mimetypes.guess_type(name)[0] or "application/octet-stream",
                size=int(path.stat().st_size),
                digest="",
                ref=f"workspace:{relative}",
                kind="workspace",
                source="workspace",
            )
        else:
            local_id = str(path)
            ref = ContentRef(
                id=local_id,
                name=name,
                mime=mimetypes.guess_type(name)[0] or "application/octet-stream",
                size=int(path.stat().st_size),
                digest="",
                ref=f"file:{path.as_posix()}",
                kind="file",
                source="local",
            )
        return ResolvedContent(
            path=path,
            ref=ref,
        )


    @staticmethod
    def _error(code: str, message: str, *, source_ref: str = "") -> ToolResult:
        normalized_code = str(code or "ocr_failed")
        metadata = {
            "error_code": normalized_code,
            "retryable": normalized_code in _RETRYABLE_ERRORS,
        }
        if source_ref:
            metadata["source_ref"] = source_ref
        return ToolResult(str(message or code), is_error=True, metadata=metadata)
