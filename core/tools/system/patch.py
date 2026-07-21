from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Dict, List

from core.tools.base import BaseTool, ToolContext, ToolResult


@dataclass
class HunkLine:
    prefix: str
    text: str
    no_newline: bool = False


@dataclass
class Hunk:
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    lines: List[HunkLine]


_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")


def parse_patch(diff_content: str) -> List[Hunk]:
    lines = str(diff_content or "").splitlines()
    hunks: List[Hunk] = []
    index = 0

    while index < len(lines):
        match = _HUNK_HEADER.match(lines[index])
        if not match:
            index += 1
            continue

        hunk = Hunk(
            old_start=int(match.group(1)),
            old_lines=int(match.group(2) or 1),
            new_start=int(match.group(3)),
            new_lines=int(match.group(4) or 1),
            lines=[],
        )
        index += 1
        while index < len(lines) and not _HUNK_HEADER.match(lines[index]):
            raw = lines[index]
            if raw == r"\ No newline at end of file":
                if not hunk.lines:
                    raise ValueError("Invalid no-newline marker.")
                hunk.lines[-1].no_newline = True
            elif raw[:1] in {" ", "+", "-"}:
                hunk.lines.append(HunkLine(raw[0], raw[1:]))
            elif raw.startswith(("--- ", "+++ ")):
                break
            else:
                raise ValueError(f"Invalid unified diff line: {raw!r}")
            index += 1

        old_count = sum(line.prefix in {" ", "-"} for line in hunk.lines)
        new_count = sum(line.prefix in {" ", "+"} for line in hunk.lines)
        if old_count != hunk.old_lines or new_count != hunk.new_lines:
            raise ValueError(
                f"Hunk count mismatch: expected -{hunk.old_lines}/+{hunk.new_lines}, "
                f"found -{old_count}/+{new_count}."
            )
        hunks.append(hunk)

    return hunks


def _newline_style(text: str) -> str:
    match = re.search(r"\r\n|\r|\n", text)
    return match.group(0) if match else "\n"


class PatchTool(BaseTool):
    SEARCH_RADIUS = 100

    @property
    def name(self) -> str:
        return "file__patch"

    @property
    def display_name(self) -> str:
        return "应用补丁"

    @property
    def description(self) -> str:
        return "Apply an exact unified diff to one existing workspace file; shifted context must match uniquely."

    @property
    def category(self) -> str:
        return "edit"

    @property
    def risk(self) -> str:
        return "medium"

    def approval_message(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        return f"Apply a patch to {arguments.get('path') or ''}?"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path to patch."},
                "diff": {"type": "string", "description": "Exact unified diff for that file."},
            },
            "required": ["path", "diff"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        path_text = str(arguments.get("path") or "").strip()
        diff = str(arguments.get("diff") or "")
        if not path_text:
            return ToolResult("path is required.", is_error=True)
        if not diff:
            return ToolResult("diff is required.", is_error=True)

        try:
            path = context.resolve_path(path_text)
        except Exception as exc:
            return ToolResult(str(exc), is_error=True)
        if not path.is_file():
            return ToolResult(f"File not found: {path_text}", is_error=True)

        try:
            hunks = parse_patch(diff)
            if not hunks:
                return ToolResult("No unified diff hunks were found.", is_error=True)
            original = path.read_bytes().decode("utf-8")
            updated = self._apply_hunks(original, hunks)
            path.write_bytes(updated.encode("utf-8"))
            return ToolResult(f"Applied patch to {path_text} ({len(hunks)} hunks).")
        except Exception as exc:
            return ToolResult(f"Patch failed: {exc}", is_error=True)

    def _apply_hunks(self, original: str, hunks: List[Hunk]) -> str:
        bom = "\ufeff" if original.startswith("\ufeff") else ""
        body = original[len(bom):]
        newline = _newline_style(body)
        had_final_newline = body.endswith(("\r", "\n"))
        lines = body.splitlines()
        line_offset = 0
        previous_old_end = 0

        for number, hunk in enumerate(hunks, start=1):
            if hunk.old_start < previous_old_end:
                raise ValueError(f"Hunk #{number} overlaps or precedes an earlier hunk.")
            previous_old_end = hunk.old_start + hunk.old_lines

            search = [line.text for line in hunk.lines if line.prefix in {" ", "-"}]
            replacement = [line.text for line in hunk.lines if line.prefix in {" ", "+"}]
            expected = max(0, min(len(lines), hunk.old_start - 1 + line_offset))

            if search:
                max_start = len(lines) - len(search)
                low = max(0, expected - self.SEARCH_RADIUS)
                high = min(max_start, expected + self.SEARCH_RADIUS)
                matches = [
                    candidate
                    for candidate in range(low, high + 1)
                    if lines[candidate:candidate + len(search)] == search
                ]
                if not matches:
                    raise ValueError(f"Could not find exact context for hunk #{number} near line {hunk.old_start}.")
                if len(matches) > 1:
                    raise ValueError(f"Hunk #{number} context matched {len(matches)} locations within +/-100 lines.")
                match_index = matches[0]
            else:
                match_index = expected

            touches_end = match_index + len(search) == len(lines)
            lines[match_index:match_index + len(search)] = replacement
            line_offset += len(replacement) - len(search)
            if touches_end:
                final_lines = [line for line in hunk.lines if line.prefix in {" ", "+"}]
                had_final_newline = bool(final_lines) and not final_lines[-1].no_newline

        rebuilt = newline.join(lines)
        if lines and had_final_newline:
            rebuilt += newline
        return bom + rebuilt
