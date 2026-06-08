"""Terminal-state helpers for the task loop."""
from __future__ import annotations

import logging
from typing import Callable, Optional

from models.conversation import Conversation, Message, normalize_tool_result

from core.task.control_messages import AUTO_CONTINUE_MODES
from core.task.types import RunPolicy, TurnContext

logger = logging.getLogger(__name__)


class TaskStopPolicy:
    """Decides when to finalize and builds terminal fallback messages."""

    @staticmethod
    def should_finalize_next_turn(
        *,
        policy: RunPolicy,
        turn_context: TurnContext,
        turns_limit: int,
    ) -> bool:
        mode_slug = str(getattr(policy, "mode", "") or "").strip().lower()
        if mode_slug not in AUTO_CONTINUE_MODES:
            return False
        return int(turn_context.turn or 0) >= max(1, int(turns_limit or 1) - 1)

    @staticmethod
    def build_max_turns_message(
        *,
        conversation: Conversation,
        final_assistant: Optional[Message],
        attach_state_snapshot: Callable[[Conversation, Message], None],
    ) -> Optional[Message]:
        if final_assistant is not None and str(getattr(final_assistant, "content", "") or "").strip():
            return final_assistant

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
            "Task loop reached the maximum number of turns before receiving `agent__complete`. "
            "Use the information already collected, increase max_turns, or narrow the request and retry."
        )
        if latest_tool_summary:
            content += f"\n\nLatest tool result summary: {latest_tool_summary[:500]}"

        final_msg = Message(role="assistant", content=content)
        final_msg.summary = content[:240]
        try:
            final_msg.metadata["completion"] = False
            final_msg.metadata["max_turns_reached"] = True
            final_msg.seq_id = conversation.next_seq_id()
        except Exception as exc:
            logger.debug("Failed to annotate max-turns message: %s", exc)
        attach_state_snapshot(conversation, final_msg)
        return final_msg
