from __future__ import annotations

import os
import platform
from typing import Any, Dict, List, Optional

from models.conversation import Conversation
from models.provider import Provider

from core.context.file_context import get_file_tree
from core.channel import build_channel_prompt_section
from core.config.schema import AppConfig
from core.modes.manager import resolve_mode_config
from core.modes.types import normalize_mode_slug
from core.skills import (
    SkillsManager,
    check_skill_execution_availability,
    resolve_skill_invocation_spec,
)


DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful and precise assistant. Follow the user's instructions carefully and ask clarifying questions when needed."
)

DEFAULT_AGENT_TOOL_GUIDELINES = (
    "## Tool Usage\n"
    "- Use the provided tools to interact with the system.\n"
    "- Always check command outputs and handle errors.\n"
    "- If a tool fails, analyze the error and try a different approach.\n"
    "- Use `execute_command` for short bounded commands. Use `shell_start` plus `shell_status`, `shell_logs`, `shell_wait`, or `shell_kill` for long-running commands.\n"
    "- Use `manage_state` to track progress and `manage_document` to keep plan/memory notes current.\n"
    "- `attempt_completion` is a built-in tool for finishing work; do not treat it as a skill or document name.\n"
    "- If another mode is a better fit, use `switch_mode`; if work should continue independently, use `new_task`."
)


def _normalize_string_tuple(values: Any) -> tuple[str, ...]:
    if isinstance(values, str):
        candidates = [part.strip() for part in values.split(",")]
    elif isinstance(values, (list, tuple, set)):
        candidates = [str(item).strip() for item in values]
    else:
        candidates = []

    normalized: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if not item or item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return tuple(normalized)


def _enabled_channel_sources(config: AppConfig) -> tuple[str, ...]:
    seen: set[str] = set()
    sources: list[str] = []
    for channel in getattr(config, "channels", []) or []:
        if not bool(getattr(channel, "enabled", False)):
            continue
        source = str(getattr(channel, "source", "") or "").strip()
        if not source or source in seen:
            continue
        seen.add(source)
        sources.append(source)
    return tuple(sources)
def build_mode_profile_section(mode_slug: str, mode_cfg: Optional[Any]) -> str:
    lines = ["<mode_profile>", f"slug: {mode_slug}"]

    if mode_cfg is not None:
        if (mode_cfg.name or "").strip():
            lines.append(f"name: {mode_cfg.name}")
        if (mode_cfg.description or "").strip():
            lines.append(f"description: {mode_cfg.description}")
        if (mode_cfg.when_to_use or "").strip():
            lines.append(f"when_to_use: {mode_cfg.when_to_use}")
        groups = sorted(mode_cfg.group_names())
        if groups:
            lines.append(f"tool_groups: {', '.join(groups)}")

    lines.append("</mode_profile>")
    return "\n".join(lines)


def build_mode_workflow_guidance(mode_slug: str) -> str:
    slug = normalize_mode_slug(mode_slug)
    guidance: dict[str, list[str]] = {
        "agent": [
            "Maintain the current todo list with `manage_state` when scope changes or steps complete.",
            "Keep a short working plan in `manage_document(name=\"plan\")` for multi-step execution.",
            "Store durable facts such as important paths, commands, or decisions in memory instead of repeating them in chat.",
            "Use `switch_mode` if the request clearly belongs to another mode, or `new_task` if a separate delegated run is better.",
            "Use `attempt_completion` only when the task is actually complete and you can summarize the result clearly.",
        ],
        "code": [
            "Before major edits, keep a concise implementation plan in `manage_document(name=\"plan\")`.",
            "Update `manage_state.tasks` as you finish concrete coding steps.",
            "Use memory for stable repo facts that matter across later turns, such as verified commands or conventions.",
            "Switch out of code mode if the task becomes primarily architecture or debugging, rather than forcing implementation prematurely.",
            "End the run with `attempt_completion`; do not use skill-loading tools as a completion signal.",
        ],
        "debug": [
            "Track active hypotheses and verification steps in `manage_document(name=\"plan\")`.",
            "Move confirmed root causes and important repro details into memory so they persist across retries and compression.",
            "Keep the todo list focused on the remaining debug actions, not on already-finished analysis.",
            "Switch to another mode once debugging is done and the remaining work is clearly implementation or planning.",
            "Call `attempt_completion` only after the root cause and fix state are explicit.",
        ],
        "plan": [
            "Create and maintain a plan document as the primary artifact for architecture work.",
            "Use the todo list to track open design questions and decision checkpoints.",
            "Persist only confirmed constraints or decisions into memory.",
            "Switch to a more appropriate mode if the task stops being architecture work, and call `attempt_completion` once the design output is ready.",
        ],
        "orchestrator": [
            "Use the plan document to track delegation strategy and aggregate results from sub-tasks.",
            "Use the todo list for the current frontier of unfinished delegated work.",
            "Use `new_task` for independent sub-work and `switch_mode` when the current conversation should continue in another mode.",
            "Call `attempt_completion` only after delegated work has been consolidated.",
        ],
    }
    items = guidance.get(slug)
    if not items:
        return ""
    return "## State Workflow\n" + "\n".join(f"- {item}" for item in items)


def build_environment_section(work_dir: str, max_depth: int = 2) -> str:
    os_info = platform.system() + " " + platform.release()
    file_tree = get_file_tree(work_dir, max_depth=max_depth)
    parts = [
        "<environment_info>",
        f"OS: {os_info}",
        f"WorkDir: {os.path.abspath(work_dir)}",
        "</environment_info>",
        "",
        "<workspace_info>",
        file_tree or "(empty)",
        "</workspace_info>",
    ]
    return "\n".join(parts).strip()


def resolve_base_system_prompt_text(
    *,
    conversation: Conversation,
    app_config: AppConfig,
    default_work_dir: str = ".",
    include_conversation_override: bool = True,
) -> str:
    settings = conversation.settings or {}
    mode_slug = normalize_mode_slug(str(getattr(conversation, "mode", "chat") or "chat"))
    work_dir = getattr(conversation, "work_dir", None) or default_work_dir

    try:
        mode_cfg = resolve_mode_config(mode_slug, work_dir=str(work_dir))
    except Exception:
        mode_cfg = None

    if include_conversation_override:
        conv_custom = ((settings.get("system_prompt") or "").strip() or (settings.get("custom_instructions") or "").strip())
        if conv_custom:
            return conv_custom

    prompt_cfg = app_config.prompts
    if mode_cfg is not None and (mode_cfg.role_definition or "").strip():
        return mode_cfg.role_definition.strip()
    if (prompt_cfg.default_system_prompt or "").strip():
        return prompt_cfg.default_system_prompt.strip()
    if (prompt_cfg.base_role_definition or "").strip():
        return prompt_cfg.base_role_definition.strip()
    return DEFAULT_SYSTEM_PROMPT


def build_state_section(conversation: Conversation) -> str:
    try:
        state = conversation.get_state()
        return state.to_prompt_view(include_documents=False) or ""
    except Exception:
        return ""


def _trim_prompt_block(value: str, *, max_chars: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def build_session_document_sections(conversation: Conversation, mode_slug: str) -> list[str]:
    try:
        state = conversation.get_state()
    except Exception:
        return []

    sections: list[str] = []
    documents = getattr(state, "documents", {}) or {}
    plan_doc = documents.get("plan")
    memory_doc = documents.get("memory")

    if plan_doc is not None and str(plan_doc.content or "").strip():
        plan_lines = ["<current_plan>"]
        plan_lines.append(_trim_prompt_block(plan_doc.content, max_chars=6000))
        plan_lines.append("</current_plan>")
        if mode_slug == "plan":
            plan_lines.append(
                "The plan document above is the primary artifact for this run. Refine it before completion and avoid implementation work in plan mode."
            )
        elif mode_slug in {"agent", "code", "debug", "orchestrator"}:
            plan_lines.append(
                "Use the current plan as the execution source of truth. Update it when scope, sequencing, or findings materially change."
            )
        sections.append("\n".join(plan_lines))

    if memory_doc is not None and str(memory_doc.content or "").strip():
        sections.append(
            "\n".join(
                [
                    "<session_memory>",
                    _trim_prompt_block(memory_doc.content, max_chars=3000),
                    "</session_memory>",
                ]
            )
        )

    other_docs: list[str] = []
    for name, doc in documents.items():
        normalized = str(name or "").strip().lower()
        if normalized in {"plan", "memory"}:
            continue
        content = str(getattr(doc, "content", "") or "").strip()
        abstract = str(getattr(doc, "abstract", "") or "").strip()
        references = [str(item).strip() for item in (getattr(doc, "references", []) or []) if str(item).strip()]
        preview_source = abstract or content
        if not preview_source:
            continue
        parts = [f"- {name}: {_trim_prompt_block(preview_source, max_chars=240)}"]
        if references:
            parts.append(f"refs={', '.join(references[:3])}")
        other_docs.append(" | ".join(parts))
    if other_docs:
        sections.append("<session_documents>\n" + "\n".join(other_docs) + "\n</session_documents>")

    return sections


def build_system_prompt(
    *,
    conversation: Conversation,
    tools: List[Dict[str, Any]],
    provider: Provider,
    app_config: AppConfig,
    default_work_dir: str = ".",
) -> str:
    settings = conversation.settings or {}

    prompt_cfg = app_config.prompts
    mode_slug = normalize_mode_slug(str(getattr(conversation, "mode", "chat") or "chat"))

    work_dir = getattr(conversation, "work_dir", None) or default_work_dir

    try:
        mode_cfg = resolve_mode_config(mode_slug, work_dir=str(work_dir))
    except Exception:
        mode_cfg = None

    conv_custom = ((settings.get("system_prompt") or "").strip() or (settings.get("custom_instructions") or "").strip())

    parts: list[str] = []

    role_def: Optional[str] = None
    mode_custom: Optional[str] = None

    if mode_cfg is not None:
        role_def = (mode_cfg.role_definition or "").strip() or None
        mode_custom = (mode_cfg.custom_instructions or "").strip() or None

    parts.append(build_mode_profile_section(mode_slug, mode_cfg))

    # System prompt precedence:
    # 1) mode.roleDefinition
    # 2) app.prompts.default_system_prompt
    # 3) app.prompts.base_role_definition (legacy)
    # 4) built-in
    if role_def:
        parts.append(role_def)
    elif (prompt_cfg.default_system_prompt or "").strip():
        parts.append(prompt_cfg.default_system_prompt.strip())
    elif (prompt_cfg.base_role_definition or "").strip():
        parts.append(prompt_cfg.base_role_definition.strip())
    else:
        parts.append(DEFAULT_SYSTEM_PROMPT)

    if (prompt_cfg.agent_tool_guidelines or "").strip():
        parts.append(prompt_cfg.agent_tool_guidelines.strip())
    else:
        parts.append(DEFAULT_AGENT_TOOL_GUIDELINES)

    # Inject available tools summary
    if tools:
        tool_lines = ["<available_tools>"]
        for t in tools:
            fn = t.get("function", {})
            tname = fn.get("name", "")
            tdesc = (fn.get("description") or "")
            if tname:
                tool_lines.append(f"- {tname}: {tdesc}")
        tool_lines.append("</available_tools>")
        parts.append("\n".join(tool_lines))

    if bool(prompt_cfg.include_state):
        state_section = build_state_section(conversation)
        if state_section:
            parts.append(state_section)

    document_sections = build_session_document_sections(conversation, mode_slug)
    if document_sections:
        parts.extend(document_sections)

    enabled_channel_sources = _enabled_channel_sources(app_config)
    allowed_channel_sources = _normalize_string_tuple(settings.get("allowed_channel_sources")) or enabled_channel_sources
    trusted_channel_sources = tuple(
        source for source in _normalize_string_tuple(settings.get("trusted_channel_sources"))
        if source in allowed_channel_sources
    )
    channel_notice_policy = str(settings.get("channel_notice_policy", "notice") or "notice").strip().lower() or "notice"
    channel_section = build_channel_prompt_section(
        getattr(conversation, "messages", []) or [],
        configured_sources=enabled_channel_sources,
        allowed_sources=allowed_channel_sources,
        trusted_sources=trusted_channel_sources,
        notice_policy=channel_notice_policy,
    )
    if channel_section:
        parts.append(channel_section)

    workflow_guidance = build_mode_workflow_guidance(mode_slug)
    if workflow_guidance:
        parts.append(workflow_guidance)

    combined_custom = "\n\n".join(
        [x for x in [mode_custom, conv_custom] if isinstance(x, str) and x.strip()]
    ).strip()
    if combined_custom:
        parts.append(f"## Custom Instructions\n{combined_custom}")

    latest_skill_run: dict[str, Any] = {}
    for msg in reversed(getattr(conversation, "messages", []) or []):
        if getattr(msg, "role", "") != "user":
            continue
        metadata = getattr(msg, "metadata", {}) or {}
        skill_run = metadata.get("skill_run") if isinstance(metadata, dict) else None
        if isinstance(skill_run, dict):
            latest_skill_run = skill_run
        break

    skill_manager = SkillsManager(getattr(conversation, "work_dir", ".") or ".")
    available_skills = []
    for skill in skill_manager.list_skills():
        spec = resolve_skill_invocation_spec(skill)
        if spec.user_invocable:
            available_skills.append(skill)
    if available_skills:
        catalog_lines = ["<available_skills>"]
        for skill in available_skills:
            spec = resolve_skill_invocation_spec(skill)
            attrs = [f'name="{skill.name}"']
            description = str(skill.description or "").strip()
            if description:
                attrs.append(f'description="{description}"')
            attrs.append(f'executor="{spec.executor}"')
            arg_hint = str(skill.metadata.get("argument-hint") or "").strip()
            if arg_hint:
                attrs.append(f'argument_hint="{arg_hint}"')
            if skill.tags:
                attrs.append(f'tags="{", ".join(skill.tags)}"')
            catalog_lines.append(f"<skill {' '.join(attrs)} />")
        catalog_lines.append("</available_skills>")
        catalog_lines.append(
            "The catalog above is for progressive skill discovery. Do not assume a skill is active or callable unless the user explicitly invoked `/{skill-name}` in this turn. Skill names are not tool names."
        )
        parts.append("\n".join(catalog_lines))

    latest_skill_name = str(latest_skill_run.get("name") or "").strip().lower()
    loaded_skill = skill_manager.get(latest_skill_name) if latest_skill_name else None
    if loaded_skill is not None:
        spec = resolve_skill_invocation_spec(loaded_skill)
        execution = check_skill_execution_availability(loaded_skill, tools)
        resource_paths = skill_manager.list_resources(loaded_skill.name)
        runtime_lines = ["<invoked_skill>"]
        runtime_lines.append(f"name: {loaded_skill.name}")
        runtime_lines.append(f"entrypoint: {loaded_skill.source}")
        runtime_lines.append(f"mode: {spec.mode}")
        runtime_lines.append(f"executor: {spec.executor}")
        runtime_lines.append(f"execution_mode: {spec.execution_mode}")
        runtime_lines.append(f"disable_model_invocation: {spec.disable_model_invocation}")
        user_input = str(latest_skill_run.get("user_input") or "").strip()
        if user_input:
            runtime_lines.append(f"user_input: {user_input}")
        if spec.preferred_cli:
            runtime_lines.append(f"preferred_cli: {', '.join(spec.preferred_cli)}")
        if spec.declared_tools:
            runtime_lines.append(f"declared_tools: {', '.join(spec.declared_tools)}")
        runtime_lines.append(f"status: {'executable' if execution.executable else 'unavailable'}")
        if execution.concrete_tools:
            runtime_lines.append(f"concrete_tools: {', '.join(execution.concrete_tools)}")
        if execution.reason:
            runtime_lines.append(f"reason: {execution.reason}")
        if execution.missing_tools:
            runtime_lines.append(f"missing_tools: {', '.join(execution.missing_tools)}")
        if resource_paths:
            runtime_lines.append(f"resource_paths: {', '.join(resource_paths[:20])}")
        runtime_lines.append("rule: Before taking action for an explicitly invoked skill, call `load_skill` to read its SKILL.md entrypoint.")
        runtime_lines.append("rule: If the loaded skill references supporting files, call `read_skill_resource` only for the specific files you need.")
        if execution.executable:
            runtime_lines.append("rule: Use only concrete tool names that appear in <available_tools> or concrete_tools. Skill names are not tool names.")
        else:
            runtime_lines.append("rule: Do not invent missing tools. If execution is unavailable, explain the missing capability and stop instead of probing repeatedly.")
        runtime_lines.append("</invoked_skill>")
        parts.append("\n".join(runtime_lines))

    return "\n\n".join([p for p in parts if isinstance(p, str) and p.strip()]).strip()
