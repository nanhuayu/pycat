import re
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional
from core.tools.base import BaseTool, ToolContext, ToolResult

# ======================================================================================
# Robust Patch Logic (Ported/Adapted from Roo Code)
# ======================================================================================

class Hunk:
    def __init__(self, old_start: int, old_lines: int, new_start: int, new_lines: int, lines: List[str]):
        self.old_start = old_start
        self.old_lines = old_lines
        self.new_start = new_start
        self.new_lines = new_lines
        self.lines = lines

def parse_patch(diff_content: str) -> List[Hunk]:
    """
    Parses a unified diff string into a list of Hunks.
    """
    hunks = []
    lines = diff_content.splitlines()
    i = 0
    
    # Regex to match hunk header: @@ -1,3 +1,4 @@
    # Note: the comma part is optional.
    header_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    
    while i < len(lines):
        line = lines[i]
        match = header_re.match(line)
        if match:
            old_start = int(match.group(1))
            old_count = int(match.group(2)) if match.group(2) is not None else 1
            new_start = int(match.group(3))
            new_count = int(match.group(4)) if match.group(4) is not None else 1
            
            hunk_lines = []
            i += 1
            # Collect lines until next hunk or end
            while i < len(lines):
                next_line = lines[i]
                if header_re.match(next_line):
                    break
                hunk_lines.append(next_line)
                i += 1
            
            hunks.append(Hunk(old_start, old_count, new_start, new_count, hunk_lines))
        else:
            i += 1
            
    return hunks

def _build_fuzzy_regex(target_lines: List[str]) -> str:
    """
    Builds a regex that matches the target lines with flexible whitespace.
    Treats the block as a sequence of lines.
    """
    regex_parts = []
    for line in target_lines:
        if not line.strip():
            # Empty line or just whitespace: match any horizontal whitespace (or empty)
            regex_parts.append(r"^\s*$")
        else:
            # Escape content, but allow variable leading/trailing whitespace
            # and collapse internal whitespace sequences to \s+
            # This is a balance between strictness and flexibility.
            
            # Escape the line
            escaped = re.escape(line.strip())
            # Allow leading whitespace (indentation) to vary? 
            # Usually we want to respect indentation, but if the user pasted code with different indent, 
            # we might want to be flexible.
            # Roo Code's fuzzy match is quite flexible.
            
            # Let's use a simpler approach: match the content with \s* around it?
            # No, that breaks structure.
            # Let's stick to: match content, tolerate whitespace differences.
            
            # Replace internal whitespace runs with \s+
            # e.g. "return  True" -> "return\s+True"
            # We need to unescape \s+ logic manually if we use re.escape first.
            # Easier: split by whitespace, escape parts, join with \s+
            parts = line.strip().split()
            line_regex = r"\s+".join([re.escape(p) for p in parts])
            
            # Allow leading indentation
            regex_parts.append(r"^\s*" + line_regex + r"\s*$")
            
    # Join lines with flexible newline matching (handling \n, \r\n)
    # We use [\r\n]+ to match one or more newline characters.
    return r"[\r\n]+".join(regex_parts)

class PatchTool(BaseTool):
    # Session-level approval cache
    _approved_paths: set = set()
    
    @property
    def name(self) -> str:
        return "apply_patch"

    @property
    def description(self) -> str:
        return "Apply a unified diff patch to a file. Supports robust hunk-based application and fuzzy matching."

    @property
    def category(self) -> str:
        return "edit"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path to patch"},
                "diff": {"type": "string", "description": "The unified diff content to apply"},
            },
            "required": ["path", "diff"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        path_str = arguments.get("path", "")
        diff = arguments.get("diff", "")
        
        if not path_str:
            return ToolResult("Missing 'path'", is_error=True)
        if not diff:
            return ToolResult("Missing 'diff'", is_error=True)

        try:
            file_path = context.resolve_path(path_str)
        except Exception as e:
            return ToolResult(str(e), is_error=True)

        if not file_path.exists():
            return ToolResult(f"File not found: {path_str}", is_error=True)

        # 1. Parse Hunks first
        try:
            hunks = parse_patch(diff)
            if not hunks:
                return ToolResult("Could not parse any hunks from the diff. Ensure it's in unified diff format.", is_error=True)
        except Exception as e:
            return ToolResult(f"Failed to parse diff: {e}", is_error=True)

        # Approval with cache
        # Ideally, we would show a diff view here.
        path_key = str(file_path)
        if path_key not in PatchTool._approved_paths:
            if not await context.ask_approval(f"Apply patch to {file_path}? ({len(hunks)} hunks)"):
                return ToolResult("User denied patch application", is_error=True)
            PatchTool._approved_paths.add(path_key)

        try:
            original_content = file_path.read_text(encoding="utf-8")
        except Exception as e:
            return ToolResult(f"Failed to read file: {e}", is_error=True)

        try:
            new_content = self._apply_hunks(original_content, hunks)
            file_path.write_text(new_content, encoding="utf-8")
            return ToolResult(f"Successfully applied patch to {path_str} ({len(hunks)} hunks)")
        except Exception as e:
            return ToolResult(f"Patch failed: {e}", is_error=True)

    def _apply_hunks(self, original: str, hunks: List[Hunk]) -> str:
        """
        Apply hunks atomically. If any hunk fails to match, raise Exception.
        """
        lines = original.splitlines(keepends=True)
        # We need to track line mapping or apply from bottom to top?
        # Actually, if we modify lines, indices shift.
        # But hunks usually have line numbers relative to original.
        # Standard patch applies sequentially and tracks offset.
        
        # However, fuzzy matching makes strict line numbers unreliable.
        # We will try to find each hunk's context in the *current* state of text (simulated).
        # But if we modify text, subsequent hunks (which are based on original line numbers) might be confused
        # if we don't adjust strictly.
        
        # Strategy:
        # 1. Locate all hunks in the original text FIRST.
        #    If any ambiguity or missing context, fail.
        # 2. Sort hunks by position (should be sorted already).
        # 3. Apply replacements from bottom to top to avoid index invalidation?
        #    No, hunks are usually top-to-bottom.
        
        # Let's do:
        # Identify the "Search Block" for each hunk.
        # Find where it matches in the file.
        # Store the (start_index, end_index, replacement_text) for each hunk.
        # Check for overlaps.
        # Apply replacements.
        
        replacements: List[Tuple[int, int, str]] = []
        
        # We need to work with a list of lines for matching
        # But wait, exact matching depends on line endings.
        # Let's normalize to list of strings (no newlines) for matching logic?
        # No, keepends=True is safer for reconstruction.
        
        # To avoid index shift issues during search, we search all first.
        # But finding Hunk 2 depends on where Hunk 1 was? 
        # Usually hunks are independent contexts. 
        # But if Hunk 1 is at line 10, Hunk 2 at line 20.
        # If we use fuzzy match, Hunk 1 might be found at line 12.
        # Then Hunk 2 should be searched after line 12.
        
        current_search_start = 0
        
        for i, hunk in enumerate(hunks):
            # Extract search block and replace block
            search_lines = []
            replace_lines = []
            
            for line in hunk.lines:
                if line.startswith(' '):
                    search_lines.append(line[1:])
                    replace_lines.append(line[1:])
                elif line == '':
                    # Handle empty lines as context (common in malformed diffs)
                    search_lines.append('')
                    replace_lines.append('')
                elif line.startswith('-'):
                    search_lines.append(line[1:])
                elif line.startswith('+'):
                    replace_lines.append(line[1:])
                # Ignore '\ No newline at end of file'
            
            # Construct strings
            # We need to match search_lines against lines[current_search_start:]
            
            match_index = -1
            match_len = 0
            
            # 1. Exact Match
            # We need to match a sequence of lines.
            # Let's optimize: brute force search in list is O(N*M).
            
            # Convert search_lines to a single string for check? 
            # Or just iterate.
            
            found = False
            
            # Try exact match first
            # We need to handle potential newline differences if we are strict.
            # Let's try to match stripped lines for "Exact-ish" match?
            # Or strictly exact.
            
            # Helper to match block
            def match_block_at(start_idx: int) -> bool:
                if start_idx + len(search_lines) > len(lines):
                    return False
                for k, s_line in enumerate(search_lines):
                    # Compare content. 
                    # Hunk lines usually don't have newlines at the end in the list, 
                    # but lines[] has keepends=True.
                    file_line = lines[start_idx + k]
                    
                    # Normalize: strip newline from file_line for comparison?
                    # s_line from hunk might be "foo\n" or "foo".
                    # Let's strip both trailing newlines for comparison.
                    if file_line.rstrip('\r\n') != s_line.rstrip('\r\n'):
                        return False
                return True

            for idx in range(current_search_start, len(lines) - len(search_lines) + 1):
                if match_block_at(idx):
                    match_index = idx
                    match_len = len(search_lines)
                    found = True
                    break
            
            # 2. Fuzzy Match (if exact failed)
            if not found:
                # Define relaxed matcher
                def match_relaxed_fuzzy_block_at(start_idx: int) -> bool:
                    if start_idx + len(search_lines) > len(lines):
                        return False
                    for k, s_line in enumerate(search_lines):
                        # Use same logic as inside loop, but defined correctly
                        f_stripped = lines[start_idx + k].strip()
                        s_stripped = s_line.strip()
                        
                        # DEBUG
                        # if start_idx < 3: # Print only for first few
                        #      print(f"DEBUG: k={k} f='{f_stripped}' s='{s_stripped}'")

                        # Relaxed matching logic:
                        # 1. Last line: always allow prefix match (common truncation at end of copy-paste)
                        # 2. Long lines (>15 chars): allow prefix match (truncation in middle of copy-paste)
                        # 3. Short lines: require exact match to avoid false positives (e.g. "if" matching "if x:")
                        
                        is_last_line = (k == len(search_lines) - 1)
                        is_long_enough = (len(s_stripped) > 15)
                        
                        if is_last_line or is_long_enough:
                            if not f_stripped.startswith(s_stripped):
                                # print(f"DEBUG: Failed prefix match at k={k}")
                                return False
                        else:
                            if f_stripped != s_stripped:
                                # print(f"DEBUG: Failed exact match at k={k}")
                                return False
                    return True

                # Try regex first for speed/robustness on whitespace
                # Reconstruct text from current search position
                remaining_text = "".join(lines[current_search_start:])
                regex_pattern = _build_fuzzy_regex(search_lines)
                flags = re.MULTILINE | re.DOTALL
                match = re.search(regex_pattern, remaining_text, flags)
                
                # If regex matched, we limit search to the vicinity? 
                # Or just use the loop. 
                # If regex fails, we STILL try the loop because regex might fail on truncation.
                
                # Scan line-by-line
                for idx in range(current_search_start, len(lines) - len(search_lines) + 1):
                    if match_relaxed_fuzzy_block_at(idx):
                        match_index = idx
                        match_len = len(search_lines)
                        found = True
                        break

            
            if not found:
                raise ValueError(f"Could not find context for hunk #{i+1} (lines {hunk.old_start}-{hunk.old_start+hunk.old_lines})")
            
            # Store replacement
            # New text
            # We need to make sure replace_lines have proper newlines?
            # Hunk replace_lines usually don't have newlines at end if we stripped them?
            # parse_patch preserves them? 
            # My parse_patch: `hunk_lines.append(next_line)` -> preserves whatever was in diff_content.
            # If diff_content lines have newlines, they are kept? 
            # usually `splitlines()` removes newlines.
            
            # So `search_lines` and `replace_lines` are without newlines.
            # But `lines` (file content) has newlines.
            
            # We need to reconstruct the replacement block WITH newlines.
            # What newline convention? Detect from file?
            # Default to \n
            
            # Use detected newline from the matched block?
            # Take the first line's ending.
            newline_char = "\n"
            if match_index < len(lines):
                m = re.search(r'(\r\n|\r|\n)$', lines[match_index])
                if m:
                    newline_char = m.group(1)
            
            replacement_text = "".join([l + newline_char for l in replace_lines])
            
            # But wait, the last line of file might not have newline.
            # This is edge case. For now, append newline.
            
            replacements.append((match_index, match_index + match_len, replacement_text))
            
            # Update search start to avoid overlap and preserve order
            current_search_start = match_index + match_len
            
        # Apply replacements from bottom to top to preserve indices
        # (Since we collected them in order, we reverse)
        replacements.sort(key=lambda x: x[0], reverse=True)
        
        current_lines = list(lines)
        
        for start, end, text in replacements:
            # Replace lines[start:end] with text (split into lines)
            # We need to split text into lines with keepends=True
            new_lines_list = text.splitlines(keepends=True)
            
            # Slice replacement
            current_lines[start:end] = new_lines_list
            
        return "".join(current_lines)
