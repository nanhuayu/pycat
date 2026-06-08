"""Runtime control prompts used by the task loop."""
from __future__ import annotations

AUTO_CONTINUE_MODES = frozenset({"agent", "code", "debug", "plan", "orchestrator"})
MAX_NUDGE_COUNT = 3

MODE_SWITCH_MESSAGE = (
    "[MODE SWITCHED] The conversation mode has changed. "
    "Continue under the new mode's responsibilities, tool set, and completion rules."
)

NUDGE_TEXT = (
    "[AUTO-CONTINUE] You responded without using any tools. "
    "If you have not completed the task, please continue using the available tools. "
    "If the task is complete, call the `agent__complete` tool to present your result. "
    "If the user asked for a document, report, timeline, or artifact, create/update it first and include the path in `agent__complete`. "
    "If a tool result says it was stored in a full-result file, do not repeatedly slice the same source; use `file__read`, "
    "call `capability__summarize` for one file or one long text, or delegate multi-file/cross-source analysis with `agent__run`. "
    "Do not simply describe what you would do - take action."
)

FINALIZE_TEXT = (
    "[FINALIZE NOW] This is the final available turn for this task loop. "
    "Stop collecting more evidence unless absolutely required. Consolidate the facts already gathered, "
    "create or update any requested report/document/artifact, and then call `agent__complete` with the final answer and file paths. "
    "Do not start a new broad search or repeat previous tool calls."
)

REPETITION_WARNING = (
    "[WARNING] You have called the same tool with identical arguments multiple times consecutively. "
    "This is not making progress. Please try a different approach, use different arguments, "
    "summarize a single long source with `capability__summarize`, delegate complex evidence with `agent__run`, "
    "or call `agent__complete` if done."
)
