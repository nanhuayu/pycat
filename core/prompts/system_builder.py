"""Build the small, stable instruction layer for one model request."""
from __future__ import annotations

from typing import Dict, List

from core.modes.manager import resolve_mode_config
from models.contracts.config import AppConfig
from models.contracts.mode import normalize_mode_slug
from models.conversation import Conversation
from models.provider import Provider


GLOBAL_PRINCIPLES = """You are PyCat, a precise desktop assistant.

- Follow the user's current request and distinguish facts from assumptions.
- Use only tools present in the request. Inspect relevant context before changing files, and verify consequential work.
- Tool failures are evidence: explain the boundary and choose a different valid path instead of repeating the same call.
- Treat `Current Time` in `<environment_info>` as authoritative. For today/latest news, weather, prices, schedules, or other time-sensitive facts, refresh with available tools, check source publication/update dates, and never label prior-day results as today; state when live verification is unavailable.
- `web__search` discovers sources; `web__fetch` reads a specific URL. Interactive browser challenges require a separately configured browser tool or another source.
- `archive__read` reads PyCat session archives; `file__read` reads workspace files.
- Delegate only focused work to `agent__run`. A sub-agent never gains permissions its parent does not have.
- State results, important verification, and any remaining limitation plainly."""


def _mode(conversation: Conversation, default_work_dir: str):
    mode_slug = normalize_mode_slug(str(getattr(conversation, "mode", "chat") or "chat"))
    work_dir = str(getattr(conversation, "work_dir", "") or default_work_dir or ".")
    try:
        return mode_slug, resolve_mode_config(mode_slug, work_dir=work_dir)
    except Exception:
        return mode_slug, None


def _join(parts: list[str]) -> str:
    return "\n\n".join(str(part or "").strip() for part in parts if str(part or "").strip())


def _completion_contract(mode) -> str:
    if str(getattr(mode, "completion_policy", "text") or "text") == "explicit":
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
            "keep exactly one in_progress, and update status as work changes; use your judgment for whether tracking helps."
        )
    if "state__artifact" in visible:
        rules.append(
            "- state__artifact: Put long plans, reports, and durable working material in an Artifact; read a relevant "
            "existing Artifact before replacing it."
        )
    if "state__memory" in visible:
        rules.append(
            "- state__memory: Save only short, stable, reusable information; never save progress, reports, raw outputs, or secrets."
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


def resolve_base_system_prompt_text(
    *,
    conversation: Conversation,
    app_config: AppConfig,
    default_work_dir: str = ".",
    include_conversation_override: bool = False,
) -> str:
    """Return stable/global/Mode instructions for read-only UI previews."""
    del include_conversation_override
    _, mode = _mode(conversation, default_work_dir)
    return _join(
        [
            GLOBAL_PRINCIPLES,
            app_config.prompts.global_instructions,
            str(getattr(mode, "prompt", "") or ""),
            _completion_contract(mode),
        ]
    )


def build_system_prompt(
    *,
    conversation: Conversation,
    tools: List[Dict],
    provider: Provider,
    app_config: AppConfig,
    default_work_dir: str = ".",
    channel_prompt_section: str = "",
    project_instruction_section: str = "",
    skill_prompt_section: str = "",
) -> str:
    """Compose stable principles followed by append-only instruction layers."""
    del provider
    settings = conversation.settings if isinstance(conversation.settings, dict) else {}
    _, mode = _mode(conversation, default_work_dir)
    session_instructions = str(settings.get("session_instructions") or "").strip()
    return _join(
        [
            GLOBAL_PRINCIPLES,
            app_config.prompts.global_instructions,
            str(getattr(mode, "prompt", "") or ""),
            _completion_contract(mode),
            _tool_usage_rules(tools),
            channel_prompt_section,
            project_instruction_section,
            session_instructions,
            skill_prompt_section,
        ]
    )
