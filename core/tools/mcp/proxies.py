from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any, Dict

from core.tools.base import BaseTool, ToolContext, ToolResult
from core.tools.mcp.naming import build_mcp_tool_name

logger = logging.getLogger(__name__)


class McpProxyTool(BaseTool):
    """Delegate execution to an external MCP server through ToolManager."""

    def __init__(self, tool_manager: Any, config: Any, tool_name: str, schema: Dict[str, Any]):
        self.tool_manager = tool_manager
        self.config = config
        self.real_tool_name = tool_name
        self._schema = schema
        self._name = build_mcp_tool_name(config.name, tool_name)

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._schema.get("description", "")

    @property
    def category(self) -> str:
        return "mcp"

    @property
    def source(self) -> str:
        return "mcp"


    @property
    def input_schema(self) -> Dict[str, Any]:
        return self._schema.get("parameters", {})

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            conversation_id = None
            try:
                if getattr(context, "conversation", None) is not None:
                    conversation_id = getattr(context.conversation, "id", None)
            except Exception as exc:
                logger.debug("Failed to resolve conversation id for MCP proxy %s: %s", self._name, exc)
                conversation_id = None

            call_arguments = self._rewrite_output_file_arguments(
                arguments,
                context=context,
                conversation_id=conversation_id,
            )

            result = await self.tool_manager.call_tool(
                self._name,
                call_arguments,
                work_dir=context.work_dir,
                conversation_id=conversation_id,
            )
            return ToolResult(self._normalize_result_paths(str(result), context=context, conversation_id=conversation_id))
        except Exception as exc:
            logger.warning("MCP proxy execution failed for %s: %s", self._name, exc)
            return ToolResult(f"MCP Tool Error: {exc}", is_error=True)

    def _session_tool_results_dir(self, *, context: ToolContext, conversation_id: object = None) -> Path | None:
        session_id = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(conversation_id or "default")).strip("-._") or "default"
        try:
            work_root = Path(context.work_dir or ".").expanduser().resolve()
        except Exception:
            return None
        return work_root / ".pycat" / "sessions" / session_id / "tool-results"

    def _rewrite_output_file_arguments(
        self,
        arguments: Dict[str, Any],
        *,
        context: ToolContext,
        conversation_id: object = None,
    ) -> Dict[str, Any]:
        """Route relative MCP output filenames into the current session.

        Browser MCP tools commonly accept a ``filename`` argument and otherwise
        write relative to the Python process cwd. Rewriting the relative output
        path before the MCP call prevents duplicate files in the project root and
        makes the returned file immediately readable from the session folder.
        """
        if not isinstance(arguments, dict):
            return arguments
        filename = arguments.get("filename")
        if not isinstance(filename, str) or not filename.strip():
            return arguments
        raw_filename = filename.strip().strip('"')
        if "://" in raw_filename:
            return arguments
        try:
            output_path = Path(raw_filename).expanduser()
        except Exception:
            return arguments
        if output_path.is_absolute():
            return arguments

        target_dir = self._session_tool_results_dir(context=context, conversation_id=conversation_id)
        if target_dir is None:
            return arguments
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            safe_name = output_path.name or "mcp-output.txt"
            rewritten = str((target_dir / safe_name).resolve())
        except Exception as exc:
            logger.debug("Failed to rewrite MCP output filename %s: %s", filename, exc)
            return arguments

        rewritten_args = dict(arguments)
        rewritten_args["filename"] = rewritten
        return rewritten_args

    def _normalize_result_paths(self, text: str, *, context: ToolContext, conversation_id: object = None) -> str:
        """Copy MCP-generated relative files into the current session.

        Some MCP servers write files relative to the Python process cwd and
        return links like ``./snapshot.md``. The agent's ``read_file`` resolves
        relative paths against ``context.work_dir``, so those links can become
        unreadable after workspace selection. Normalize such links into
        ``<work_dir>/.pycat/sessions/<conversation_id>/tool-results``.
        """
        if not text:
            return text

        target_dir = self._session_tool_results_dir(context=context, conversation_id=conversation_id)
        if target_dir is None:
            return text
        try:
            work_root = Path(context.work_dir or ".").expanduser().resolve()
        except Exception:
            return text
        process_root = Path.cwd().resolve()

        def resolve_source(raw_path: str) -> Path | None:
            normalized = raw_path.strip().strip("<>").replace("/", "\\")
            if not normalized or "://" in normalized:
                return None
            source = Path(normalized)
            candidates = []
            if source.is_absolute():
                candidates.append(source)
            else:
                candidates.append((work_root / source).resolve())
                candidates.append((process_root / source).resolve())
            for candidate in candidates:
                try:
                    if candidate.is_file():
                        return candidate
                except Exception:
                    continue
            return None

        def copy_source(source: Path) -> str:
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                target = (target_dir / source.name).resolve()
                if source.resolve() != target:
                    shutil.copy2(source, target)
                return str(target)
            except Exception as exc:
                logger.debug("Failed to normalize MCP result file %s: %s", source, exc)
                return str(source)

        def replace_markdown(match: re.Match[str]) -> str:
            prefix = match.group(1)
            raw_path = match.group(2)
            source = resolve_source(raw_path)
            if not source:
                return match.group(0)
            return f"{prefix}{copy_source(source)})"

        text = re.sub(r"(\[[^\]]+\]\()([^\)]+)\)", replace_markdown, text)

        def replace_plain(match: re.Match[str]) -> str:
            raw_path = match.group(0)
            source = resolve_source(raw_path)
            if not source:
                return raw_path
            return copy_source(source)

        return re.sub(r"(?<![\w:/\\])(?:\.\\|\./|\.playwright-mcp[\\/])[^\s\]\)]+", replace_plain, text)