"""Tool repetition detector for the agent loop.

Detects when the LLM calls the same tool with identical arguments
consecutively, which typically indicates a stuck loop.
"""
from __future__ import annotations

import hashlib
import json
from collections import deque
from typing import Any, Dict


class ToolRepetitionDetector:
    """Track consecutive identical tool calls.

    Usage::

        detector = ToolRepetitionDetector(threshold=3)
        if detector.record("read_file", {"path": "/a.py"}):
            # inject warning — same call repeated `threshold` times
            ...
    """

    def __init__(self, threshold: int = 3) -> None:
        self._threshold = max(2, threshold)
        self._history: deque[str] = deque(maxlen=threshold)

    def record(self, tool_name: str, arguments: Dict[str, Any]) -> bool:
        """Record a tool call. Returns True if repetition threshold reached."""
        key = self._make_key(tool_name, arguments)
        self._history.append(key)
        if len(self._history) < self._threshold:
            return False
        return all(k == key for k in self._history)

    def reset(self) -> None:
        self._history.clear()

    @staticmethod
    def _make_key(tool_name: str, arguments: Dict[str, Any]) -> str:
        try:
            args_str = json.dumps(arguments, sort_keys=True, default=str)
        except (TypeError, ValueError):
            args_str = str(arguments)
        h = hashlib.md5(args_str.encode()).hexdigest()[:12]
        return f"{tool_name}:{h}"
