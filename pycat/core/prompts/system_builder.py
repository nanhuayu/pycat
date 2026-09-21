"""Build the small, stable instruction layer for one model request."""
from __future__ import annotations

from typing import Dict, List

from pycat.core.modes.manager import resolve_mode_config
from pycat.core.prompts.sections import PromptSections
from pycat.models.contracts.config import AppConfig
from pycat.models.contracts.agent import effective_pycat_assistant_enabled
from pycat.models.contracts.mode import normalize_mode_slug
from pycat.models.conversation import Conversation
from pycat.models.provider import Provider


GLOBAL_PRINCIPLES = """You are PyCat, a precise desktop assistant.

- Follow the user's current request and distinguish facts from assumptions.
- Use only tools present in the request. Inspect relevant context before changing files, and verify consequential work.
- Tool failures are evidence: explain the boundary and choose a different valid path instead of repeating the same call.
- Treat `captured_at` on the tail `<current_state>` as the request snapshot time. For today/latest news, weather, prices, schedules, or other time-sensitive facts, refresh with available tools, check source publication/update dates, and never label prior-day results as today; state when live verification is unavailable.
- `web__search` discovers sources; `web__fetch` reads a specific URL. Interactive browser challenges require a separately configured browser tool or another source.
- Delegate only focused work to `agent__run`. A sub-agent never gains permissions its parent does not have.
- State results, important verification, and any remaining limitation plainly."""


def _mode(conversation: Conversation, default_work_dir: str):
    mode_slug = normalize_mode_slug(str(getattr(conversation, "mode", "chat") or "chat"))
    work_dir = str(getattr(conversation, "work_dir", "") or default_work_dir or "")
    try:
        return mode_slug, resolve_mode_config(mode_slug, work_dir=work_dir, data_dir=getattr(conversation, "data_dir", None))
    except Exception:
        return mode_slug, None


def _join(parts: list[str]) -> str:
    return "\n\n".join(str(part or "").strip() for part in parts if str(part or "").strip())


def _completion_contract(mode, completion_policy: str | None = None) -> str:
    """Render the contract from the request's effective policy.

    Prompt previews do not have a ``RunPolicy`` and fall back to the mode
    configuration. Runtime requests pass the immutable policy explicitly so
    an entry-point override cannot drift from the instructions shown to the
    model.
    """
    effective_policy = str(
        completion_policy
        if completion_policy not in (None, "")
        else getattr(mode, "completion_policy", "text")
        or "text"
    ).strip().lower()
    if effective_policy == "explicit":
        return (
            "Completion: ordinary assistant text is progress, not completion. "
            "When the task is fully finished, call agent__complete with the final result."
        )
    return "Completion: a normal assistant response without tool calls completes the run."


def _tool_names(tools: List[Dict]) -> set[str]:
    names: set[str] = set()
    for tool in tools or []:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = str(function.get("name") or "").strip() if isinstance(function, dict) else ""
        if name:
            names.add(name)
    return names


def _tool_usage_rules(tools: List[Dict]) -> str:
    visible = _tool_names(tools)
    rules: list[str] = []
    if "state__todo" in visible:
        rules.append(
            "- state__todo: When work genuinely benefits from tracking, define 2-4 outcome milestones, "
            "keep at most one in_progress, and update status as work changes; use your judgment for whether tracking helps."
        )
    if "state__artifact" in visible:
        rules.append(
            "- state__artifact: Put long plans, reports, and durable working material in an Artifact; read a relevant "
            "existing Artifact before replacing it."
        )
    if "state__memory" in visible:
        rules.append(
            "- state__memory: If the user explicitly asks you to remember a stable preference or project fact, save it now. "
            "Use read for stable entry IDs and the current digest before editing. Keep entries short and reusable; "
            "never save progress, reports, raw outputs, or secrets. Project memory requires a workspace; user preferences use target=user."
        )
    if "state__wiki" in visible:
        rules.append("- state__wiki: Search relevant project knowledge before repeating an investigation. Store synthesized conclusions, "
                     "conditions and limits with pinned source references; keep session deliverables in artifacts.")
    if "shell__run" in visible:
        rules.append(
            "- shell__run: Waits are bounded; a command that outlives the wait is NOT killed — it keeps "
            "running in background and returns a process_id. For known long-running work pass wait_seconds=0 "
            "to go background immediately; poll with shell__read(wait_seconds=..., cursor=...), never re-run "
            "a command that is already running, and shell__kill processes that are no longer needed."
        )
    if "archive__read" in visible:
        rules.append(
            "- archive__read: Reads exact or summarized PyCat session content by content_id; "
            "file__read reads filesystem paths instead."
        )
    if visible.intersection({"file__deliver", "capability__image"}):
        rules.append(
            "- Image delivery: Embed delivered images directly in the final Markdown response, including "
            "agent__complete.result, using ![description](<returned image ref>). Reuse the exact workspace: or archive: "
            "reference returned by a successful tool; do not invent paths or return only a filename. "
            "Follow an explicit structured output schema instead when one is required."
        )
    if not rules:
        return ""
    return "\n".join(
        [
            "<tool_usage_rules>",
            "The complete tool catalog is provided separately in the request tools field.",
            *rules,
            "</tool_usage_rules>",
        ]
    )


def _prompt_mode_and_assistant(
    conversation: Conversation,
    default_work_dir: str,
    pycat_assistant_enabled: bool | None,
):
    settings = conversation.settings if isinstance(conversation.settings, dict) else {}
    mode_slug, mode = _mode(conversation, default_work_dir)
    assistant_enabled = effective_pycat_assistant_enabled(
        pycat_assistant_enabled
        if pycat_assistant_enabled is not None
        else settings.get("pycat_assistant_enabled", True),
        mode=mode_slug,
    )
    return mode, assistant_enabled


def resolve_base_system_prompt_text(
    *,
    conversation: Conversation,
    app_config: AppConfig,
    default_work_dir: str = "",
    include_conversation_override: bool = False,
    pycat_assistant_enabled: bool | None = None,
    completion_policy: str | None = None,
) -> str:
    """Return stable/global/Mode instructions for read-only UI previews."""
    del include_conversation_override
    mode, assistant_enabled = _prompt_mode_and_assistant(
        conversation,
        default_work_dir,
        pycat_assistant_enabled,
    )
    mode_prompt = str(getattr(mode, "prompt", "") or "") if assistant_enabled else ""
    completion_contract = _completion_contract(mode, completion_policy) if assistant_enabled else ""
    return _join(
        [
            GLOBAL_PRINCIPLES if assistant_enabled else "",
            app_config.prompts.global_instructions,
            mode_prompt,
            completion_contract,
        ]
    )


def build_system_prompt(
    *,
    conversation: Conversation,
    tools: List[Dict],
    provider: Provider,
    app_config: AppConfig,
    default_work_dir: str = "",
    sections: PromptSections | None = None,
    pycat_assistant_enabled: bool | None = None,
    completion_policy: str | None = None,
) -> str:
    """Compose stable principles followed by append-only instruction layers."""
    del provider
    settings = conversation.settings if isinstance(conversation.settings, dict) else {}
    mode, assistant_enabled = _prompt_mode_and_assistant(
        conversation,
        default_work_dir,
        pycat_assistant_enabled,
    )
    sections = sections or PromptSections()
    session_instructions = str(settings.get("session_instructions") or "").strip()
    return _join(
        [
            GLOBAL_PRINCIPLES if assistant_enabled else "",
            app_config.prompts.global_instructions,
            str(getattr(mode, "prompt", "") or "") if assistant_enabled else "",
            _completion_contract(mode, completion_policy) if assistant_enabled else "",
            _tool_usage_rules(tools) if assistant_enabled else "",
            sections.channel,
            sections.project_instructions if assistant_enabled else "",
            session_instructions,
            sections.skills if assistant_enabled else "",
        ]
    )
