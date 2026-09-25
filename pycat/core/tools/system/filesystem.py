import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

from pycat.core.content.attachments import encode_image_file_to_data_url
from pycat.core.content.mime import DEFAULT_MIME, guess_mime
from pycat.core.content.office import extract_office_text, is_office_attachment
from pycat.core.content.pdf import MAX_PDF_BYTES, extract_pdf_text_isolated
from pycat.core.content.references import build_workspace_content_ref
from pycat.core.content.resolver import SessionContentResolver
from pycat.core.tools.base import BaseTool, ToolContext, ToolResult
from pycat.core.tools.system.file_search import search_files
from pycat.models.contracts.channel import channel_file_delivery_enabled


class LsTool(BaseTool):
    @property
    def name(self) -> str:
        return "file__list"

    @property
    def display_name(self) -> str:
        return "列出文件"

    @property
    def description(self) -> str:
        return "List files and directories under an authorized path on the workspace host with a bounded result count."

    @property
    def category(self) -> str:
        return "read"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Authorized path; defaults to workspace or home."},
                "recursive": {"type": "boolean", "description": "Include descendants; default false."},
                "limit": {"type": "integer", "description": "Maximum entries; default 200, max 2000."},
            },
            "additionalProperties": False,
        }

    def requested_read_path(self, arguments: Dict[str, Any]) -> str:
        return str(arguments.get("path") or ".")

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            base_path = await asyncio.to_thread(context.resolve_read_path, str(arguments.get("path") or "."))
            limit = max(1, min(int(arguments.get("limit") or 200), 2000))
        except Exception as exc:
            return ToolResult(f"Invalid argument: {exc}", is_error=True)
        if context.files:
            try:
                result = await asyncio.to_thread(context.files.call, "list", base_path,
                    recursive=bool(arguments.get("recursive")), limit=limit)
                for entry in result["entries"]:
                    entry["path"] = context.display_path(entry["path"])
                    entry["type"] = "dir" if entry["type"] == "directory" else "file"
                return ToolResult(json.dumps(result, ensure_ascii=False))
            except Exception as exc:
                return ToolResult(f"List error: {exc}", is_error=True)
        if not base_path.exists():
            return ToolResult(f"Not found: {base_path}", is_error=True)

        paths = base_path.rglob("*") if base_path.is_dir() and arguments.get("recursive") else (
            base_path.iterdir() if base_path.is_dir() else iter((base_path,))
        )
        entries: List[Dict[str, Any]] = []
        try:
            for path in sorted(paths, key=lambda item: str(item).lower()):
                try:
                    authorized = context.resolve_read_path(str(path))
                    relative = context.display_path(path).replace("\\", "/")
                    size = int(authorized.stat().st_size)
                except Exception:
                    continue
                entries.append({
                    "path": relative,
                    "type": "dir" if authorized.is_dir() else "file",
                    "size": size,
                })
                if len(entries) >= limit:
                    break
        except Exception as exc:
            return ToolResult(f"List error: {exc}", is_error=True)
        return ToolResult(json.dumps({"entries": entries, "truncated": len(entries) >= limit}, ensure_ascii=False, indent=2))


class ReadFileTool(BaseTool):
    MAX_LINES_PER_READ = 2000

    @property
    def name(self) -> str:
        return "file__read"

    @property
    def display_name(self) -> str:
        return "读取文件"

    @property
    def description(self) -> str:
        return (
            "Read an authorized file or current-session input snapshot. Text supports line ranges; "
            "PDFs return native text, image counts and next_page in bounded page batches, without OCR. "
            "Use file__ocr for text within images or scanned pages."
        )

    @property
    def category(self) -> str:
        return "read"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Authorized path on the workspace host or current-session input:<id> reference.",
                },
                "start_line": {"type": "integer", "description": "Optional 1-based start line."},
                "end_line": {"type": "integer", "description": "Optional inclusive end line."},
                "start_page": {"type": "integer", "minimum": 1, "description": "PDF only: 1-based start page; default 1."},
                "page_count": {"type": "integer", "minimum": 1, "maximum": 32,
                               "description": "PDF only: pages in this batch; default 5, maximum 32. Follow next_page."},
            },
            "required": ["path"],
            "additionalProperties": False,
        }

    def requested_read_path(self, arguments: Dict[str, Any]) -> str:
        path = str(arguments.get("path") or "").strip()
        return "" if path.startswith("input:") else path

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        path_text = str(arguments.get("path") or "").strip()
        if not path_text:
            return ToolResult("path is required.", is_error=True)
        try:
            content_name = ""
            content_mime = ""
            if path_text.startswith("input:"):
                if context.conversation is None:
                    return ToolResult("input references require an active conversation.", is_error=True)
                content_service = getattr(context, "content_service", None)
                if content_service is None:
                    return ToolResult("input references require the session content service.", is_error=True)
                resolved = SessionContentResolver(content_service).resolve_content(
                    context.conversation,
                    path_text,
                )
                file_path = resolved.path
                content_name = resolved.name
                content_mime = resolved.mime
            else:
                file_path = await asyncio.to_thread(context.resolve_read_path, path_text)
                if context.files:
                    file_path = await asyncio.to_thread(context.workspace_service.materialize, context.conversation,
                        str(file_path), files=context.files, max_bytes=20 * 1024 * 1024)
        except Exception as exc:
            return ToolResult(str(exc), is_error=True)
        if not file_path.is_file():
            return ToolResult(f"Not a file: {file_path}", is_error=True)

        try:
            size = file_path.stat().st_size
            mime_type = content_mime
            if not mime_type or mime_type.lower() == DEFAULT_MIME:
                mime_type = guess_mime(content_name or file_path)
            if str(mime_type or "").startswith("image/"):
                if size > 20 * 1024 * 1024:
                    return ToolResult("Image file is larger than 20 MB.", is_error=True)
                image_url = encode_image_file_to_data_url(str(file_path))
                if not image_url:
                    return ToolResult(f"Failed to encode image: {path_text}", is_error=True)
                return ToolResult([
                    {"type": "text", "text": f"Image: {path_text}\nMime-Type: {mime_type}\nSize: {size} bytes"},
                    {"type": "image", "mimeType": mime_type or "image/png", "data": image_url},
                ])

            if is_office_attachment(content_name or file_path.name, str(mime_type or "")):
                if size > 10 * 1024 * 1024:
                    return ToolResult(
                        "Office file is larger than 10 MB; use a smaller source or the desktop application.",
                        is_error=True,
                    )
                extracted = extract_office_text(
                    file_path.read_bytes(),
                    name=content_name or file_path.name,
                    mime=str(mime_type),
                    max_bytes=1024 * 1024,
                )
                suffix = "\n[truncated=true]" if extracted.truncated else ""
                return ToolResult(extracted.text + suffix)

            content_suffix = Path(content_name or file_path.name).suffix.lower()
            if str(mime_type or "").lower() == "application/pdf" or content_suffix == ".pdf":
                if size > MAX_PDF_BYTES:
                    return ToolResult('PDF file is larger than 20 MB; use a smaller source.', is_error=True)
                extracted = await asyncio.to_thread(
                    extract_pdf_text_isolated, file_path,
                    start_page=int(arguments.get('start_page', 1)),
                    page_count=int(arguments.get('page_count', 5)),
                )
                extracted['source_ref'] = path_text
                if not context.files:
                    extracted['local_path'] = str(file_path.resolve())
                return ToolResult(json.dumps(extracted, ensure_ascii=False))

            if size > 10 * 1024 * 1024:
                return ToolResult("File is larger than 10 MB; use file__search or a smaller source.", is_error=True)
            text = file_path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines(keepends=True)
            total = len(lines)
            if arguments.get("start_line") is not None or arguments.get("end_line") is not None:
                start = max(1, int(arguments.get("start_line") or 1))
                end = min(total, int(arguments.get("end_line") or total))
                if start > end:
                    return ToolResult(f"Invalid line range: {start}-{end}", is_error=True)
                return ToolResult(f"Lines {start}-{end} of {total}:\n{''.join(lines[start - 1:end])}")
            if total > self.MAX_LINES_PER_READ:
                body = "".join(lines[: self.MAX_LINES_PER_READ])
                return ToolResult(
                    f"Lines 1-{self.MAX_LINES_PER_READ} of {total}:\n{body}\n\n"
                    f"Continue with start_line={self.MAX_LINES_PER_READ + 1}."
                )
            return ToolResult(text)
        except Exception as exc:
            return ToolResult(f"Read error: {exc}", is_error=True)


class GrepTool(BaseTool):
    @property
    def name(self) -> str:
        return "file__search"

    @property
    def display_name(self) -> str:
        return "搜索文件"

    @property
    def description(self) -> str:
        return "Search authorized text files on the workspace host using ripgrep when available, otherwise bounded Python literal search. Regex requires rg on that host. Skips hidden, linked, binary and files over 2 MiB. Remote Python fallback reports ignore_files=false because ignore-file rules require remote rg. Narrow path/glob for large projects."

    @property
    def category(self) -> str:
        return "read"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text or regular expression to find."},
                "path": {"type": "string", "description": "Authorized directory; defaults to workspace or home."},
                "glob": {"type": "string", "description": "Optional include glob such as '**/*.py'."},
                "regex": {"type": "boolean", "description": "Use ripgrep (Rust) regex syntax; requires rg installed. Default false."},
                "limit": {"type": "integer", "description": "Maximum matches; default 50, max 500."},
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    def requested_read_path(self, arguments: Dict[str, Any]) -> str:
        return str(arguments.get("path") or ".")

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        query = str(arguments.get("query") or "")
        if not query:
            return ToolResult("query is required.", is_error=True)
        try:
            root = await asyncio.to_thread(context.resolve_read_path, str(arguments.get("path") or "."))
            limit = max(1, min(int(arguments.get("limit") or 50), 500))
        except Exception as exc:
            return ToolResult(f"Invalid argument: {exc}", is_error=True)
        if not context.file_is_dir(root):
            return ToolResult(f"Not a directory: {root}", is_error=True)

        try:
            if context.files:
                result = await asyncio.to_thread(context.files.call, "search", root, query=query,
                    regex=bool(arguments.get("regex")), glob=str(arguments.get("glob") or ""), limit=limit)
                for match in result["matches"]:
                    match["path"] = context.display_path(match["path"])
                return ToolResult(json.dumps(result, ensure_ascii=False))
            result = await search_files(root=root, context=context, query=query, regex=bool(arguments.get('regex')),
                                        glob=str(arguments.get('glob') or ''), limit=limit)
            return ToolResult(json.dumps(result, ensure_ascii=False, indent=2))
        except TimeoutError:
            return ToolResult('File search timed out; narrow the path or glob.', is_error=True)
        except (OSError, ValueError) as exc:
            return ToolResult(f'File search failed: {exc}', is_error=True)


class DeliverFilesTool(BaseTool):
    @property
    def name(self) -> str:
        return "file__deliver"

    @property
    def display_name(self) -> str:
        return "交付文件"

    @property
    def description(self) -> str:
        return "Declare existing workspace files as final outputs. In a supported bound channel, these files are sent to that conversation's recipient."

    @property
    def category(self) -> str:
        return "read"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 50,
                    "description": "Workspace-relative paths to completed output files.",
                },
            },
            "required": ["paths"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        source = str(getattr(getattr(context, "runtime", None), "source", "") or "")
        channel_delivery = source == 'channel' and channel_file_delivery_enabled(
            getattr(context.conversation, 'settings', {}) or {})
        if source not in {"desktop", "cli", "sdk"} and not channel_delivery:
            return ToolResult("file__deliver requires a local root run or a channel supporting file delivery.", is_error=True)
        work_dir = str(getattr(getattr(context, "conversation", None), "work_dir", "") or "").strip()
        if not work_dir:
            return ToolResult("file__deliver requires an active workspace.", is_error=True)
        paths = arguments.get("paths")
        if not isinstance(paths, list) or not paths or len(paths) > 50:
            return ToolResult("paths must contain between 1 and 50 file paths.", is_error=True)
        def build_refs():
            refs = []
            for raw_path in paths:
                path = str(raw_path or "").strip()
                if not path:
                    raise ValueError("file path is empty")
                refs.append(build_workspace_content_ref(work_dir, path, workspace_service=context.workspace_service))
            return refs

        try:
            refs = await asyncio.to_thread(build_refs)
        except (OSError, ValueError) as exc:
            return ToolResult(str(exc), is_error=True)
        return ToolResult(
            json.dumps(
                {"delivered": [ref.ref for ref in refs]},
                ensure_ascii=False,
            ),
            metadata={"content_refs": [ref.to_dict() for ref in refs]},
        )
