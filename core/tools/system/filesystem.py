import asyncio
import json
import mimetypes
import re
from pathlib import Path
from typing import Any, Dict, List

from core.content.attachments import encode_image_file_to_data_url
from core.content.office import extract_office_text, is_office_attachment
from core.content.references import build_workspace_content_ref
from core.tools.base import BaseTool, ToolContext, ToolResult


class LsTool(BaseTool):
    @property
    def name(self) -> str:
        return "file__list"

    @property
    def display_name(self) -> str:
        return "列出文件"

    @property
    def description(self) -> str:
        return "List files and directories under a workspace path with a bounded result count."

    @property
    def category(self) -> str:
        return "read"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative path; default '.'."},
                "recursive": {"type": "boolean", "description": "Include descendants; default false."},
                "limit": {"type": "integer", "description": "Maximum entries; default 200, max 2000."},
            },
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            base_path = context.resolve_path(str(arguments.get("path") or "."))
            limit = max(1, min(int(arguments.get("limit") or 200), 2000))
        except Exception as exc:
            return ToolResult(f"Invalid argument: {exc}", is_error=True)
        if not base_path.exists():
            return ToolResult(f"Not found: {base_path}", is_error=True)

        workspace = Path(context.work_dir).resolve()
        paths = base_path.rglob("*") if base_path.is_dir() and arguments.get("recursive") else (
            base_path.iterdir() if base_path.is_dir() else iter((base_path,))
        )
        entries: List[Dict[str, Any]] = []
        try:
            for path in sorted(paths, key=lambda item: str(item).lower()):
                try:
                    relative = str(path.relative_to(workspace)).replace("\\", "/")
                except Exception:
                    relative = str(path)
                try:
                    size = int(path.stat().st_size)
                except Exception:
                    size = None
                entries.append({
                    "path": relative,
                    "type": "dir" if path.is_dir() else "file",
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
        return "Read a bounded workspace file or current-session input snapshot; text can be limited to an inclusive line range."

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
                    "description": "Workspace-relative file path or current-session input:<id> reference.",
                },
                "start_line": {"type": "integer", "description": "Optional 1-based start line."},
                "end_line": {"type": "integer", "description": "Optional inclusive end line."},
            },
            "required": ["path"],
            "additionalProperties": False,
        }

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
                content_ref = content_service.load_ref(context.conversation, path_text)
                file_path = content_service.resolve_original(context.conversation, content_ref)
                content_name = str(content_ref.name or "")
                content_mime = str(content_ref.mime or "")
            else:
                file_path = context.resolve_path(path_text)
        except Exception as exc:
            return ToolResult(str(exc), is_error=True)
        if not file_path.is_file():
            return ToolResult(f"Not a file: {file_path}", is_error=True)

        try:
            size = file_path.stat().st_size
            guessed_mime = mimetypes.guess_type(content_name or str(file_path))[0]
            mime_type = content_mime
            if not mime_type or mime_type.lower() == "application/octet-stream":
                mime_type = guessed_mime or mime_type
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
                    mime=str(mime_type or "application/octet-stream"),
                    max_bytes=1024 * 1024,
                )
                suffix = "\n[truncated=true]" if extracted.truncated else ""
                return ToolResult(extracted.text + suffix)

            content_suffix = Path(content_name or file_path.name).suffix.lower()
            if str(mime_type or "").lower() == "application/pdf" or content_suffix == ".pdf":
                return ToolResult(
                    "PDF text extraction is not configured; open the original file or provide a text export.",
                    is_error=True,
                )

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
        return "Search workspace text files using literal text or an explicit regular expression."

    @property
    def category(self) -> str:
        return "read"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text or regular expression to find."},
                "path": {"type": "string", "description": "Workspace-relative directory; default '.'."},
                "glob": {"type": "string", "description": "Optional include glob such as '**/*.py'."},
                "regex": {"type": "boolean", "description": "Interpret query as regex; default false."},
                "limit": {"type": "integer", "description": "Maximum matches; default 50, max 500."},
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        query = str(arguments.get("query") or "")
        if not query:
            return ToolResult("query is required.", is_error=True)
        try:
            root = context.resolve_path(str(arguments.get("path") or "."))
            pattern = re.compile(query if arguments.get("regex") else re.escape(query))
            limit = max(1, min(int(arguments.get("limit") or 50), 500))
        except re.error as exc:
            return ToolResult(f"Invalid regex: {exc}", is_error=True)
        except Exception as exc:
            return ToolResult(f"Invalid argument: {exc}", is_error=True)
        if not root.is_dir():
            return ToolResult(f"Not a directory: {root}", is_error=True)

        workspace = Path(context.work_dir).resolve()
        matches: List[Dict[str, Any]] = []
        for path in root.glob(str(arguments.get("glob") or "**/*")):
            if not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except Exception:
                continue
            for line_number, line in enumerate(lines, 1):
                if pattern.search(line):
                    try:
                        relative = str(path.relative_to(workspace)).replace("\\", "/")
                    except Exception:
                        relative = str(path)
                    matches.append({"path": relative, "line": line_number, "text": line[:300]})
                    if len(matches) >= limit:
                        return ToolResult(json.dumps({"matches": matches, "truncated": True}, ensure_ascii=False, indent=2))
        return ToolResult(json.dumps({"matches": matches, "truncated": False}, ensure_ascii=False, indent=2))


class DeliverFilesTool(BaseTool):
    @property
    def name(self) -> str:
        return "file__deliver"

    @property
    def display_name(self) -> str:
        return "交付文件"

    @property
    def description(self) -> str:
        return "Declare existing workspace files as final outputs for the current local task without reading their contents."

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
        if source not in {"desktop", "cli"}:
            return ToolResult("file__deliver is available only to Desktop or CLI root runs.", is_error=True)
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
                refs.append(build_workspace_content_ref(work_dir, path))
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
