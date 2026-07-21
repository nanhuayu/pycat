import shutil
import re
import unicodedata
from typing import Any, Dict

from core.tools.base import BaseTool, ToolContext, ToolResult


def _canonical_units(text: str) -> tuple[str, dict[int, int]]:
    """Normalize only the equivalences accepted by file__edit."""
    canonical: list[str] = []
    boundaries: dict[int, int] = {}
    index = 1 if text.startswith("\ufeff") else 0
    canonical_offset = 0
    boundaries[0] = index

    while index < len(text):
        start = index
        if text.startswith("\r\n", index):
            index += 2
            normalized = "\n"
        elif text[index] in {"\r", "\n"}:
            index += 1
            normalized = "\n"
        else:
            index += 1
            while index < len(text) and unicodedata.combining(text[index]):
                index += 1
            normalized = unicodedata.normalize("NFD", text[start:index])

        boundaries[canonical_offset] = start
        canonical.append(normalized)
        canonical_offset += len(normalized)
        boundaries[canonical_offset] = index

    return "".join(canonical), boundaries


def _equivalent_match_spans(content: str, old_text: str) -> list[tuple[int, int]]:
    canonical_content, boundaries = _canonical_units(content)
    canonical_old, _ = _canonical_units(old_text)
    if not canonical_old:
        return []

    spans: set[tuple[int, int]] = set()
    start = canonical_content.find(canonical_old)
    while start >= 0:
        end = start + len(canonical_old)
        if start in boundaries and end in boundaries:
            spans.add((boundaries[start], boundaries[end]))
        start = canonical_content.find(canonical_old, start + 1)
    return sorted(spans)


def _preferred_newline(content: str, matched: str) -> str:
    for source in (matched, content):
        endings = re.findall(r"\r\n|\r|\n", source)
        if endings:
            return max(("\r\n", "\n", "\r"), key=lambda item: endings.count(item))
    return "\n"


def _adapt_newlines(text: str, newline: str) -> str:
    return re.sub(r"\r\n|\r|\n", newline, text)


class WriteToFileTool(BaseTool):
    @property
    def name(self) -> str:
        return "file__write"

    @property
    def display_name(self) -> str:
        return "写入文件"

    @property
    def description(self) -> str:
        return "Create or replace one workspace text file with the supplied content."

    @property
    def category(self) -> str:
        return "edit"

    @property
    def risk(self) -> str:
        return "medium"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path."},
                "content": {"type": "string", "description": "Complete file content."},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        }

    def approval_message(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        return f"Write file {arguments.get('path') or ''}?"

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        path_text = str(arguments.get("path") or "").strip()
        if not path_text:
            return ToolResult("path is required.", is_error=True)
        try:
            path = context.resolve_path(path_text)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(arguments.get("content") or ""), encoding="utf-8")
            return ToolResult(f"Wrote {path_text}")
        except Exception as exc:
            return ToolResult(f"Write error: {exc}", is_error=True)


class EditFileTool(BaseTool):
    @property
    def name(self) -> str:
        return "file__edit"

    @property
    def display_name(self) -> str:
        return "精确编辑文件"

    @property
    def description(self) -> str:
        return "Replace one uniquely matching text block in an existing workspace file."

    @property
    def category(self) -> str:
        return "edit"

    @property
    def risk(self) -> str:
        return "medium"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path."},
                "old_text": {"type": "string", "description": "Exact text that must occur once."},
                "new_text": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        }

    def approval_message(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        return f"Edit file {arguments.get('path') or ''}?"

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        path_text = str(arguments.get("path") or "").strip()
        old_text = str(arguments.get("old_text") or "")
        new_text = str(arguments.get("new_text") or "")
        if not path_text or not old_text:
            return ToolResult("path and non-empty old_text are required.", is_error=True)
        try:
            path = context.resolve_path(path_text)
        except Exception as exc:
            return ToolResult(str(exc), is_error=True)
        if not path.is_file():
            return ToolResult(f"File not found: {path_text}", is_error=True)
        try:
            content = path.read_bytes().decode("utf-8")
            spans = _equivalent_match_spans(content, old_text)
            if not spans:
                return ToolResult("old_text was not found.", is_error=True)
            if len(spans) > 1:
                return ToolResult(f"old_text matched {len(spans)} locations; provide a unique block.", is_error=True)
            start, end = spans[0]
            replacement = _adapt_newlines(new_text, _preferred_newline(content, content[start:end]))
            if content.startswith("\ufeff") and start == 1 and replacement.startswith("\ufeff"):
                replacement = replacement[1:]
            path.write_bytes((content[:start] + replacement + content[end:]).encode("utf-8"))
            return ToolResult(f"Edited {path_text}")
        except Exception as exc:
            return ToolResult(f"Edit error: {exc}", is_error=True)


class DeleteFileTool(BaseTool):
    @property
    def name(self) -> str:
        return "file__delete"

    @property
    def display_name(self) -> str:
        return "删除文件"

    @property
    def description(self) -> str:
        return "Delete one workspace file or directory; non-empty directories require recursive=true."

    @property
    def category(self) -> str:
        return "edit"

    @property
    def risk(self) -> str:
        return "high"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative path."},
                "recursive": {"type": "boolean", "description": "Allow deletion of a non-empty directory."},
            },
            "required": ["path"],
            "additionalProperties": False,
        }

    def approval_message(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        return f"Permanently delete {arguments.get('path') or ''}?"

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        path_text = str(arguments.get("path") or "").strip()
        if not path_text:
            return ToolResult("path is required.", is_error=True)
        try:
            path = context.resolve_path(path_text)
        except Exception as exc:
            return ToolResult(str(exc), is_error=True)
        if not path.exists():
            return ToolResult(f"Path not found: {path_text}", is_error=True)
        try:
            if path.is_dir():
                if any(path.iterdir()) and not bool(arguments.get("recursive")):
                    return ToolResult("Directory is not empty; set recursive=true to delete it.", is_error=True)
                shutil.rmtree(path)
            else:
                path.unlink()
            return ToolResult(f"Deleted {path_text}")
        except Exception as exc:
            return ToolResult(f"Delete error: {exc}", is_error=True)
