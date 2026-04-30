import json
import os
from typing import Any, Dict
from core.tools.base import BaseTool, ToolContext, ToolResult

class WriteToFileTool(BaseTool):
    # Session-level approval cache to avoid repeated prompts
    _approved_paths: set = set()
    
    @property
    def name(self) -> str:
        return "write_to_file"

    @property
    def description(self) -> str:
        return "Write content to a file. Overwrites existing files."

    @property
    def category(self) -> str:
        return "edit"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path"},
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        path_str = arguments.get("path", "")
        content = arguments.get("content", "")
        
        if not path_str:
            return ToolResult("Missing 'path'", is_error=True)

        try:
            file_path = context.resolve_path(path_str)
        except Exception as e:
            return ToolResult(str(e), is_error=True)

        # Check approval cache first to avoid repeated prompts
        path_key = str(file_path)
        if path_key not in WriteToFileTool._approved_paths:
            if not await context.ask_approval(f"Write to file {file_path}?"):
                return ToolResult("User denied file write", is_error=True)
            WriteToFileTool._approved_paths.add(path_key)

        try:
            # Ensure parent directory exists
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            return ToolResult(f"Successfully wrote to {path_str}")
        except Exception as e:
            return ToolResult(f"Write error: {e}", is_error=True)


import re

# ======================================================================================
# Roo Code inspired helpers for robust file editing
# ======================================================================================

def count_occurrences(text: str, substring: str) -> int:
    if not substring:
        return 0
    return text.count(substring)

def normalize_to_lf(content: str) -> str:
    return content.replace("\r\n", "\n")

def build_whitespace_tolerant_regex(target: str) -> str:
    """
    Builds a regex that matches the target string but allows flexible whitespace.
    Adapted from Roo Code's EditFileTool.ts.
    """
    if not target:
        return ""
    
    # Split by whitespace sequences (keeping them)
    # \s matches [ \t\n\r\f\v], \S matches non-whitespace
    parts = re.findall(r'(\s+|\S+)', target)
    if not parts:
        return ""
    
    regex_parts = []
    for part in parts:
        if re.match(r'^\s+$', part):
            # If the whitespace run includes a newline, allow matching any whitespace (including newlines)
            # to tolerate wrapping changes across lines.
            if '\n' in part or '\r' in part:
                regex_parts.append(r'\s+')
            else:
                # Limit matching to horizontal whitespace so we don't accidentally consume
                # line breaks that precede indentation.
                regex_parts.append(r'[ \t]+')
        else:
            regex_parts.append(re.escape(part))
            
    return "".join(regex_parts)


class EditFileTool(BaseTool):
    def __init__(self):
        super().__init__()
        self._approved_paths = set()

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return "Edit a file by replacing a text segment with a new one, or by applying a unified diff. Supports whitespace-tolerant matching (Roo Code style)."

    @property
    def category(self) -> str:
        return "edit"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path"},
                "old_str": {"type": "string", "description": "Text to find and replace (exact match or whitespace-tolerant). Required if diff is not provided."},
                "new_str": {"type": "string", "description": "New text to insert. Required if diff is not provided."},
                "diff": {"type": "string", "description": "Unified diff content to apply. If provided, old_str/new_str are ignored."},
            },
            # We relax required fields to allow either old_str/new_str OR diff
            "required": ["path"], 
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        path_str = arguments.get("path", "")
        old_str = arguments.get("old_str", "")
        new_str = arguments.get("new_str", "")
        diff = arguments.get("diff", "")

        if not path_str:
            return ToolResult("Missing 'path'", is_error=True)
        
        try:
            file_path = context.resolve_path(path_str)
        except Exception as e:
            return ToolResult(str(e), is_error=True)

        # Mode Selection
        is_patch_mode = bool(diff)
        
        # Check file existence
        if not file_path.exists():
            if not is_patch_mode and not old_str:
                # Creation mode (only if using string replacement mode without old_str, implying new file)
                # But typically creation is done via WriteToFile. 
                # EditFileTool with empty old_str could mean "append" or "create", but usually creation is strict.
                # If old_str is empty and new_str is present, and file doesn't exist, we might allow creation?
                # Roo Code EditFileTool usually requires file to exist for search/replace.
                # WriteToFile is for creation.
                pass # Fall through to error
            
            return ToolResult(f"File not found: {path_str}", is_error=True)
        
        if not is_patch_mode and not old_str:
             return ToolResult("Missing 'old_str' for existing file. Use 'builtin_filesystem_write' to overwrite, or provide 'diff' for patch mode.", is_error=True)

        # Approval
        action_desc = "Apply patch to" if is_patch_mode else "Edit file"
        if str(file_path) not in self._approved_paths:
            if not await context.ask_approval(f"{action_desc} {file_path}?"):
                return ToolResult(f"User denied file {action_desc.lower()}", is_error=True)
            self._approved_paths.add(str(file_path))

        try:
            content = file_path.read_text(encoding="utf-8")
            
            if is_patch_mode:
                # Apply Patch
                # Inline the logic for now to ensure self-contained EditFileTool
                new_content = self._apply_diff(content, diff)
                file_path.write_text(new_content, encoding="utf-8")
                return ToolResult(f"Successfully applied patch to {path_str}")

            # 1. Try Exact Match
            if old_str in content:
                # Verify uniqueness if possible, but for now replace first occurrence
                count = content.count(old_str)
                if count > 1:
                     # Warn? Or just proceed? Roo Code warns.
                     pass 
                
                new_content = content.replace(old_str, new_str, 1)
                file_path.write_text(new_content, encoding="utf-8")
                return ToolResult(f"Successfully edited {path_str} (Exact match)")

            # 2. Try Whitespace-Tolerant Regex Match
            regex_pattern = build_whitespace_tolerant_regex(old_str)
            if not regex_pattern:
                 return ToolResult("Could not build regex from old_str", is_error=True)
            
            match = re.search(regex_pattern, content)
            if not match:
                return ToolResult("Could not find old_str in file (tried exact and whitespace-tolerant match).", is_error=True)
            
            span = match.span()
            new_content = content[:span[0]] + new_str + content[span[1]:]
            
            # Write back
            file_path.write_text(new_content, encoding="utf-8")
            return ToolResult(f"Successfully edited {path_str} (Regex match)")
            
        except Exception as e:
            return ToolResult(f"Edit error: {e}", is_error=True)

    def _apply_diff(self, original: str, diff: str) -> str:
        """
        Apply a unified diff string to the original content.
        Supports fuzzy matching by ignoring whitespace in context lines.
        """
        lines = original.splitlines(keepends=True)
        diff_lines = diff.splitlines(keepends=True)
        hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
        
        current_lines = list(lines)
        i = 0
        
        def normalize_line(line: str) -> str:
            return line.strip()

        while i < len(diff_lines):
            line = diff_lines[i]
            match = hunk_re.match(line)
            if match:
                hunk_content = []
                i += 1
                while i < len(diff_lines) and not hunk_re.match(diff_lines[i]):
                    hunk_content.append(diff_lines[i])
                    i += 1
                
                search_block = []
                replace_block = []
                for hline in hunk_content:
                    if hline.startswith(' '):
                        search_block.append(hline[1:])
                        replace_block.append(hline[1:])
                    elif hline.startswith('-'):
                        search_block.append(hline[1:])
                    elif hline.startswith('+'):
                        replace_block.append(hline[1:])
                
                search_str = "".join(search_block)
                replace_str = "".join(replace_block)
                
                original_text = "".join(current_lines)
                
                # 1. Exact match
                if search_str in original_text:
                    current_lines = original_text.replace(search_str, replace_str, 1).splitlines(keepends=True)
                    continue

                # 2. Fuzzy match (ignore whitespace)
                # This is computationally expensive for large files, but acceptable for typical code files.
                # We try to find a block in original_text that matches search_block when normalized.
                
                found = False
                # Use a sliding window or regex? Regex is easier.
                # Construct a regex that matches search_block with flexible whitespace
                
                search_regex_parts = []
                for s_line in search_block:
                    # Escape the line, but replace whitespace sequences with \s+
                    escaped = re.escape(s_line.strip())
                    # Allow leading/trailing whitespace on lines to vary? 
                    # Usually diff context preserves indentation, but maybe tabs vs spaces changed.
                    # Let's be lenient: match the content, ignore surrounding whitespace
                    search_regex_parts.append(r"\s*" + escaped + r"\s*")
                
                # Join lines with \s* (newlines are whitespace)
                # But we want to match line-by-line structure
                search_regex = r"\s*".join(search_regex_parts)
                
                # This regex might be too loose. Let's try to match line content exactly but ignore indentation.
                # Actually, build_whitespace_tolerant_regex logic is better.
                
                # Let's try using build_whitespace_tolerant_regex on the whole block
                fuzzy_regex = build_whitespace_tolerant_regex(search_str)
                if fuzzy_regex:
                    match_obj = re.search(fuzzy_regex, original_text)
                    if match_obj:
                        span = match_obj.span()
                        # Replace the found block with replace_str
                        # Note: replace_str assumes the indentation in the diff is correct.
                        # If we fuzzy matched and ignored indentation in search, we might break indentation in replace.
                        # This is a risk. But for "Patch mode is better for large modifications", usually diffs are accurate.
                        
                        current_lines = (original_text[:span[0]] + replace_str + original_text[span[1]:]).splitlines(keepends=True)
                        found = True
                
                if not found:
                    raise ValueError("Could not find context for hunk in file (tried exact and fuzzy match).")
            else:
                i += 1
        return "".join(current_lines)




class DeleteFileTool(BaseTool):
    @property
    def name(self) -> str:
        return "builtin_filesystem_delete"

    @property
    def description(self) -> str:
        return "Delete a file or directory."

    @property
    def category(self) -> str:
        return "edit"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative path"},
                "recursive": {"type": "boolean", "description": "Delete directory recursively (default false)"},
            },
            "required": ["path"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        path_str = arguments.get("path", "")
        recursive = bool(arguments.get("recursive", False))

        if not path_str:
            return ToolResult("Missing 'path'", is_error=True)

        try:
            file_path = context.resolve_path(path_str)
        except Exception as e:
            return ToolResult(str(e), is_error=True)

        if not file_path.exists():
            return ToolResult(f"Path not found: {path_str}", is_error=True)

        if not await context.ask_approval(f"DELETE {file_path}?"):
            return ToolResult("User denied deletion", is_error=True)

        try:
            if file_path.is_dir():
                if not recursive and any(file_path.iterdir()):
                    return ToolResult("Directory not empty. Use recursive=true to delete.", is_error=True)
                import shutil
                shutil.rmtree(file_path)
            else:
                file_path.unlink()
            return ToolResult(f"Successfully deleted {path_str}")
        except Exception as e:
            return ToolResult(f"Delete error: {e}", is_error=True)
