"""Short runtime recovery messages for the agent loop."""
from __future__ import annotations

REPETITION_WARNING = (
    "[WARNING] The same tool was called repeatedly with identical arguments. "
    "Use different evidence or arguments. When the task is complete, follow the current completion contract "
    "(agent__complete in explicit mode)."
)

MAX_EXPLICIT_COMPLETION_RETRIES = 2

EXPLICIT_COMPLETION_REMINDER = (
    "[RUNTIME] This mode requires explicit completion. Continue the unfinished work, then call "
    "agent__complete(result=...) when the final result is ready."
)

RESUME_UNFINISHED_RUN = (
    "[RUNTIME] Resume the unfinished task from the existing conversation and state. "
    "Reuse completed tool results and continue the remaining work. "
    "Follow the current mode's completion contract."
)
