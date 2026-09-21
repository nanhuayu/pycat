"""Terminal-state helpers for the task loop."""
from __future__ import annotations

import logging
from typing import Callable, Optional

from pycat.models.conversation import Conversation, Message, normalize_tool_result

logger = logging.getLogger(__name__)


class TaskStopPolicy:
    """Decides when to finalize and builds terminal fallback messages."""

    @staticmethod
    def build_max_turns_message(
        *,
        conversation: Conversation,
        final_assistant: Optional[Message],
        attach_state_snapshot: Callable[[Conversation, Message], None],
    ) -> Optional[Message]:
        latest_tool_summary = ""
        try:
            for msg in reversed(getattr(conversation, "messages", []) or []):
                if getattr(msg, "role", "") != "assistant":
                    continue
                for tool_call in reversed(getattr(msg, "tool_calls", None) or []):
                    if not isinstance(tool_call, dict) or "result" not in tool_call:
                        continue
                    result = normalize_tool_result(tool_call.get("result"))
                    latest_tool_summary = str(result.get("summary") or result.get("content") or "").strip()
                    if latest_tool_summary:
                        break
                if latest_tool_summary:
                    break
        except Exception:
            latest_tool_summary = ""

        content = (
            "Agent run was interrupted after reaching the maximum number of turns before explicit completion. "
            "The conversation, tool results, Todo, Memory, Artifact, and archive state were preserved and can be continued."
        )
        if latest_tool_summary:
            content += f"\n\nLatest tool result summary: {latest_tool_summary[:500]}"

        final_msg = Message(role="assistant", content=content)
        final_msg.summary = content[:240]
        try:
            final_msg.metadata["completion"] = False
            final_msg.metadata["interrupted"] = True
            final_msg.metadata["interrupt_reason"] = "max_turns"
            final_msg.metadata["max_turns_reached"] = True
        except Exception as exc:
            logger.debug("Failed to annotate max-turns message: %s", exc)
        attach_state_snapshot(conversation, final_msg)
        return final_msg
