"""Tool result processing pipeline.

Replaces the monolithic ``_maybe_spill_tool_result`` in ``Task`` with a
small, conservative pipeline that keeps tool output faithful: short results
are passed through, and oversized results are saved to disk with a bounded
head/tail preview.

Key concepts:
- ``ResultHandle``: unified representation of a processed tool result.
- ``ResultDisposition``: strategies (INLINE, SPILL).
- ``ToolResultPipeline``: router + executor for all strategies.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ResultDisposition(Enum):
    """How a tool result should be handled before being shown to the LLM."""

    INLINE = "inline"       # Pass through as-is
    SPILL = "spill"         # Save to disk, return preview + path hint


@dataclass
class ResultHandle:
    """The outcome of processing a raw tool result.

    Attributes
    ----------
    display:
        Text that the LLM actually sees.
    full_path:
        Path to the full raw content on disk (``None`` if not spilled).
    total_chars:
        Length of the *original* raw text.
    is_processed:
        ``True`` if the result was shortened for display or saved as an
        external artifact. The raw result is never cleaned or summarized by
        this pipeline.
    strategy:
        Name of the disposition that was applied.
    hint:
        Optional guidance for the LLM (e.g. how to read the rest).
    """

    display: str
    full_path: Optional[str] = None
    total_chars: int = 0
    is_processed: bool = False
    strategy: str = "inline"
    hint: Optional[str] = None


class ToolResultPipeline:
    """Classify and post-process tool results.

    The pipeline is **stateless** except for the spill directory path.
    It can be instantiated once per ``Task`` and reused across turns.
    """

    # ------------------------------------------------------------------
    # Configurable thresholds
    # ------------------------------------------------------------------
    SPILL_THRESHOLD_CHARS = 32_000
    SPILL_PREVIEW_CHARS = 8_000

    # ------------------------------------------------------------------
    # Built-in tool -> disposition mapping
    # ------------------------------------------------------------------
    BUILT_IN_STRATEGIES: dict[str, ResultDisposition] = {
        # Read/search tools: pagination is handled at the tool layer.
        "read_file": ResultDisposition.INLINE,
        "search_content": ResultDisposition.INLINE,
        "grep": ResultDisposition.INLINE,
        "list_directory": ResultDisposition.INLINE,
        "ls": ResultDisposition.INLINE,
        "skill__read_resource": ResultDisposition.INLINE,

        # Web tools can return large remote content.
        "web_search": ResultDisposition.SPILL,
        "web_fetch": ResultDisposition.SPILL,

        # File mutation tools return compact status messages.
        "file_write": ResultDisposition.INLINE,
        "file_edit": ResultDisposition.INLINE,
        "file_delete": ResultDisposition.INLINE,
        "file_patch": ResultDisposition.INLINE,

        # Command / execution tools: output is unbounded -> spill when long.
        "execute_command": ResultDisposition.SPILL,
        "shell_start": ResultDisposition.SPILL,
        "shell_logs": ResultDisposition.SPILL,
        "shell_status": ResultDisposition.INLINE,
        "shell_wait": ResultDisposition.INLINE,
        "shell_kill": ResultDisposition.INLINE,
        "python_exec": ResultDisposition.SPILL,

        # State / management tools return structured, bounded state updates.
        "manage_state": ResultDisposition.INLINE,
        "manage_memory": ResultDisposition.INLINE,
        "manage_todo": ResultDisposition.INLINE,
        "manage_artifact": ResultDisposition.INLINE,
        "ask_questions": ResultDisposition.INLINE,

        # Skill / delegation / control tools return bounded metadata.
        "skill__load": ResultDisposition.INLINE,
        "subagent__read_analyze": ResultDisposition.INLINE,
        "subagent__search": ResultDisposition.INLINE,
        "subagent__custom": ResultDisposition.INLINE,
        "attempt_completion": ResultDisposition.INLINE,
        "switch_mode": ResultDisposition.INLINE,
    }

    def __init__(self, work_dir: str, conversation_id: object = None):
        self.work_dir = Path(work_dir).resolve()
        session_id = self._safe_session_id(conversation_id)
        self.spill_dir = self.work_dir / ".pycat" / "sessions" / session_id / "tool-results"

    @staticmethod
    def _safe_session_id(conversation_id: object = None) -> str:
        raw = str(conversation_id or "default").strip() or "default"
        return re.sub(r"[^a-zA-Z0-9_.-]+", "-", raw).strip("-._") or "default"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(
        self,
        tool_name: str,
        raw_text: str,
        tool_call_id: Optional[str] = None,
        seq_id: int = 0,
    ) -> ResultHandle:
        """Process a raw tool result and return a ``ResultHandle``."""
        text = str(raw_text or "")
        disposition = self._classify(tool_name, text)

        if disposition == ResultDisposition.INLINE:
            return self._inline(text)
        if disposition == ResultDisposition.SPILL:
            return self._spill(tool_name, text, tool_call_id, seq_id)

        return self._inline(text)

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def _classify(self, tool_name: str, text: str) -> ResultDisposition:
        """Pick a disposition for *tool_name* and *text*."""
        # Built-in tools
        if tool_name in self.BUILT_IN_STRATEGIES:
            disp = self.BUILT_IN_STRATEGIES[tool_name]
            if disp == ResultDisposition.SPILL:
                if len(text) <= self.SPILL_THRESHOLD_CHARS:
                    return ResultDisposition.INLINE
            return disp

        # Capability tools (e.g. capability__translate) schedule sub-tasks;
        # their direct output is a short scheduling acknowledgement.
        if tool_name.startswith("capability__"):
            return ResultDisposition.INLINE

        # Default sub-agent tools (e.g. subagent__read_analyze) schedule child
        # tasks; their direct output is a short scheduling acknowledgement.
        if tool_name.startswith("subagent__"):
            return ResultDisposition.INLINE

        # MCP proxies: preserve raw data. Never auto-clean or extract here.
        if tool_name.startswith("mcp__"):
            if len(text) <= self.SPILL_THRESHOLD_CHARS:
                return ResultDisposition.INLINE
            return ResultDisposition.SPILL

        # Fallback
        if len(text) <= self.SPILL_THRESHOLD_CHARS:
            return ResultDisposition.INLINE
        return ResultDisposition.SPILL

    # ------------------------------------------------------------------
    # Strategy implementations
    # ------------------------------------------------------------------

    def _inline(self, text: str) -> ResultHandle:
        return ResultHandle(
            display=text,
            total_chars=len(text),
            is_processed=False,
            strategy="inline",
        )

    def _spill(
        self,
        tool_name: str,
        text: str,
        tool_call_id: Optional[str],
        seq_id: int,
    ) -> ResultHandle:
        try:
            self.spill_dir.mkdir(parents=True, exist_ok=True)
            safe_tool = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(tool_name or "tool")).strip("_") or "tool"
            call_id = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(tool_call_id or "call")).strip("_")[:40] or "call"
            target_path = (self.spill_dir / f"{seq_id:06d}_{safe_tool}_{call_id}.txt").resolve()

            # Safety: ensure the resolved path is still inside spill_dir
            if self.spill_dir not in target_path.parents:
                return self._fallback_truncate(text)

            target_path.write_text(text, encoding="utf-8", errors="replace")
            preview = self._head_tail_preview(text).rstrip()
            hint = f"Use read_file to access the complete output: {target_path}"

            display = (
                f"{preview}\n\n"
                f"... [full raw result stored at {target_path}; {len(text)} chars. "
                "Content was not cleaned or summarized.] ...\n\n"
                f"{hint}"
            )

            return ResultHandle(
                display=display,
                full_path=str(target_path),
                total_chars=len(text),
                is_processed=True,
                strategy="spill",
                hint=hint,
            )
        except Exception as exc:
            logger.warning("Failed to spill tool result for %s: %s", tool_name, exc)
            return self._fallback_truncate(text)

    def _fallback_truncate(self, text: str) -> ResultHandle:
        """Safe fallback when disk write fails: keep head + tail."""
        display = self._head_tail_preview(text)
        return ResultHandle(
            display=display,
            total_chars=len(text),
            is_processed=True,
            strategy="fallback_truncate",
            hint="Content truncated. Original could not be saved to file.",
        )

    @staticmethod
    def _head_tail_preview(text: str) -> str:
        if len(text) <= ToolResultPipeline.SPILL_PREVIEW_CHARS:
            return text
        half = ToolResultPipeline.SPILL_PREVIEW_CHARS // 2
        omitted = max(0, len(text) - ToolResultPipeline.SPILL_PREVIEW_CHARS)
        return (
            text[:half]
            + f"\n\n... [preview omitted {omitted} chars; raw content preserved on disk if spill succeeded] ...\n\n"
            + text[-half:]
        )
