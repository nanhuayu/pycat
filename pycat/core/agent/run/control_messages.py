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

RESUME_INTERRUPTED_RUN = (
    "[RUNTIME] Resume the interrupted task from the existing conversation and state. "
    "Continue the remaining work and call agent__complete(result=...) when finished."
)
