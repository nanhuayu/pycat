import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Dict, List

from core.attachments import encode_image_file_to_data_url
from core.tools.base import BaseTool, ToolContext, ToolResult

class LsTool(BaseTool):
    @property
    def name(self) -> str:
        return "list_files"

    @property
    def description(self) -> str:
        return "List files and directories under a workspace path."

    @property
    def category(self) -> str:
        return "read"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative path. Default: '.'"},
                "recursive": {"type": "boolean", "description": "List recursively. Default: false"},
                "maxEntries": {"type": "number", "description": "Limit returned entries. Default: 200"},
            },
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        path_str = arguments.get("path", ".")
        recursive = bool(arguments.get("recursive", False))
        max_entries = int(arguments.get("maxEntries", 200) or 200)
        max_entries = max(1, min(max_entries, 2000))

        try:
            base_path = context.resolve_path(path_str)
        except Exception as e:
            return ToolResult(str(e), is_error=True)

        if not base_path.exists():
            return ToolResult(f"Not found: {base_path}", is_error=True)

        entries: List[Dict[str, Any]] = []
        effective_root = Path(context.work_dir).resolve()

        def add_entry(p: Path):
            try:
                rel = str(p.relative_to(effective_root)).replace("\\", "/")
            except Exception:
                rel = str(p)
            try:
                stat = p.stat()
                size = int(stat.st_size)
            except Exception:
                size = None
            entries.append({
                "path": rel,
                "name": p.name,
                "type": "dir" if p.is_dir() else "file",
                "size": size,
            })

        try:
            if base_path.is_dir():
                if recursive:
                    for p in base_path.rglob("*"):
                        add_entry(p)
                        if len(entries) >= max_entries:
                            break
                else:
                    for p in base_path.iterdir():
                        add_entry(p)
                        if len(entries) >= max_entries:
                            break
            else:
                add_entry(base_path)
        except Exception as e:
            return ToolResult(f"List error: {e}", is_error=True)

        return ToolResult(json.dumps({
            "root": str(effective_root).replace("\\", "/"),
            "entries": entries
        }, ensure_ascii=False, indent=2))


class ReadFileTool(BaseTool):
    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "Read a workspace file. Text files support line ranges; image files can be returned as multimodal content blocks."

    @property
    def category(self) -> str:
        return "read"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path"},
                "start_line": {"type": "number", "description": "Optional start line (1-based, inclusive)"},
                "end_line": {"type": "number", "description": "Optional end line (1-based, inclusive)"},
                "mode": {"type": "string", "description": "Optional read mode: auto, text, or image. Default: auto"},
            },
            "required": ["path"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        path_str = str(arguments.get("path", "") or "").strip()
        start_line = arguments.get("start_line")
        end_line = arguments.get("end_line")
        mode = str(arguments.get("mode", "auto") or "auto").strip().lower()

        if not path_str:
            return ToolResult("Missing 'path'", is_error=True)

        try:
            file_path = context.resolve_path(path_str)
        except Exception as e:
            return ToolResult(str(e), is_error=True)

        if not file_path.is_file():
            return ToolResult(f"Not a file: {file_path}", is_error=True)

        try:
            file_size = file_path.stat().st_size
            mime_type, _ = mimetypes.guess_type(str(file_path))
            is_image = str(mime_type or "").startswith("image/")

            if mode in {"auto", "image"} and is_image:
                if file_size > 20 * 1024 * 1024:
                    return ToolResult("Image file too large (>20MB).", is_error=True)
                image_url = encode_image_file_to_data_url(str(file_path))
                if not image_url:
                    return ToolResult(f"Read error: failed to encode image {file_path}", is_error=True)
                rel_path = str(file_path.relative_to(Path(context.work_dir).resolve())).replace("\\", "/")
                return ToolResult(
                    [
                        {
                            "type": "text",
                            "text": f"Image file: {rel_path}\nMime-Type: {mime_type or 'image/png'}\nSize: {file_size} bytes",
                        },
                        {
                            "type": "image",
                            "mimeType": mime_type or "image/png",
                            "data": image_url,
                        },
                    ]
                )

            if mode == "image" and not is_image:
                return ToolResult(f"File is not an image: {file_path}", is_error=True)

            # Read entire file (Python handles buffering generally well for moderate files)
            # For massive files, we should optimize, but for now this is consistent with Roo Code logic
            # which often reads whole file then slices.
            # Roo Code has a limit of 10MB or so.
            if file_size > 10 * 1024 * 1024:
                return ToolResult("File too large (>10MB). Use grep or read specific lines.", is_error=True)

            text = file_path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines(keepends=True) # Keep ends to preserve structure
            total_lines = len(lines)
            
            # Slice mode
            if start_line is not None or end_line is not None:
                start = int(start_line) if start_line is not None else 1
                end = int(end_line) if end_line is not None else total_lines
                
                start = max(1, start)
                end = min(total_lines, end)
                
                if start > end:
                    return ToolResult(f"Invalid range: start_line ({start}) > end_line ({end})", is_error=True)
                
                # Adjust to 0-based
                sliced_lines = lines[start-1:end]
                content = "".join(sliced_lines)
                
                return ToolResult(f"Lines {start}-{end} of {total_lines}:\n{content}")
            
            return ToolResult(text)
        except Exception as e:
            return ToolResult(f"Read error: {e}", is_error=True)


class GrepTool(BaseTool):
    @property
    def name(self) -> str:
        return "search_files"

    @property
    def description(self) -> str:
        return "Search for a regex in text files under a workspace directory."

    @property
    def category(self) -> str:
        return "read"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative directory. Default: '.'"},
                "pattern": {"type": "string", "description": "Regex pattern"},
                "include": {"type": "string", "description": "Optional glob include pattern, e.g. '**/*.py'"},
                "maxMatches": {"type": "number", "description": "Max matches (default: 50)"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        root_str = arguments.get("path", ".")
        pattern = arguments.get("pattern", "")
        include = arguments.get("include")
        max_matches = int(arguments.get("maxMatches", 50) or 50)
        max_matches = max(1, min(max_matches, 500))

        if not pattern:
            return ToolResult("Missing 'pattern'", is_error=True)

        try:
            root_path = context.resolve_path(root_str)
        except Exception as e:
            return ToolResult(str(e), is_error=True)

        if not root_path.exists() or not root_path.is_dir():
            return ToolResult(f"Not a directory: {root_path}", is_error=True)

        if not await context.ask_approval(f"Grep in {root_str} for '{pattern}'?"):
            return ToolResult("User denied grep", is_error=True)

        try:
            rx = re.compile(pattern)
        except Exception as e:
            return ToolResult(f"Invalid regex: {e}", is_error=True)

        glob_pattern = include if isinstance(include, str) and include.strip() else "**/*"
        matches: List[Dict[str, Any]] = []
        effective_root = Path(context.work_dir).resolve()

        for p in root_path.glob(glob_pattern):
            if len(matches) >= max_matches:
                break
            if not p.is_file():
                continue
            try:
                content = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for i, line in enumerate(content.splitlines(), 1):
                if rx.search(line):
                    try:
                        rel = str(p.relative_to(effective_root)).replace("\\", "/")
                    except Exception:
                        rel = str(p)
                    matches.append({"path": rel, "line": i, "text": line[:300]})
                    if len(matches) >= max_matches:
                        break
        
        return ToolResult(json.dumps({
            "matches": matches, 
            "count": len(matches)
        }, ensure_ascii=False, indent=2))
